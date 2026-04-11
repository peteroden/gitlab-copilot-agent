"""Git clone with retry logic for transient failures."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import structlog

from gitlab_copilot_agent.git.validation import (
    is_transient_clone_error,
    sanitize_url_for_log,
    validate_clone_url,
)
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)

CLONE_DIR_PREFIX = "mr-review-"


class TransientCloneError(RuntimeError):
    """Raised when git clone fails after exhausting retries on transient errors."""

    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


async def git_clone(
    clone_url: str,
    branch: str,
    token: str,
    *,
    clone_dir: str | None = None,
    max_retries: int = 3,
    backoff_base: float = 5.0,
) -> Path:
    """Clone repo to temp dir with retry on transient failures. Returns path.

    Embeds credentials in the clone URL. This is acceptable because the service
    runs as a single non-root user inside a Docker container — /proc/pid/cmdline
    and /proc/pid/environ are only readable by the same uid. The token never
    reaches disk (.git/config uses the temp dir which is cleaned up on error,
    and the origin URL is not used after clone). Error messages are sanitized.

    Args:
        clone_url: HTTPS URL to clone (must not contain embedded credentials)
        branch: Branch name to checkout
        token: GitLab token for authentication
        clone_dir: Base directory for the clone temp dir
        max_retries: Maximum retry attempts for transient failures (default: 3)
        backoff_base: Base interval in seconds for exponential backoff (default: 5.0)

    Raises:
        ValueError: If clone_url is invalid or contains embedded credentials
        TransientCloneError: If clone fails after exhausting retries on transient errors
        RuntimeError: If git clone fails with a non-transient error
    """
    with _tracer.start_as_current_span("git.clone", attributes={"branch": branch}) as span:
        validate_clone_url(clone_url)
        safe_url = sanitize_url_for_log(clone_url)

        auth_url = clone_url.replace("https://", f"https://oauth2:{token}@")
        _allow_http = os.environ.get("ALLOW_HTTP_CLONE", "").lower() in ("true", "1", "yes")
        clone_args = ["git", "clone"]
        if not _allow_http:
            clone_args.append("--depth=1")
        clone_args.extend(["--branch", branch, "--", auth_url])

        last_error = ""
        for attempt in range(1, max_retries + 1):
            tmp_dir = Path(tempfile.mkdtemp(prefix=CLONE_DIR_PREFIX, dir=clone_dir))
            attempt_args = [*clone_args, str(tmp_dir)]
            proc = await asyncio.create_subprocess_exec(
                *attempt_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                shutil.rmtree(tmp_dir, ignore_errors=True)
                last_error = "git clone timed out after 120s"
                if attempt < max_retries:
                    backoff = backoff_base * (3 ** (attempt - 1))
                    await log.awarning(
                        "git_clone_retry",
                        attempt=attempt,
                        max_retries=max_retries,
                        error=last_error,
                        backoff_seconds=backoff,
                        url=safe_url,
                    )
                    await asyncio.sleep(backoff)
                    continue
                span.set_attribute("clone.attempts", attempt)
                span.set_attribute("clone.outcome", "transient_failure")
                raise TransientCloneError(
                    f"git clone failed for {safe_url} after {attempt} attempts: {last_error}",
                    attempts=attempt,
                ) from exc

            if proc.returncode == 0:
                span.set_attribute("clone.attempts", attempt)
                span.set_attribute("clone.outcome", "success")
                await log.ainfo("repo_cloned", path=str(tmp_dir), branch=branch, attempt=attempt)
                return tmp_dir

            shutil.rmtree(tmp_dir, ignore_errors=True)
            raw_stderr = stderr.decode().strip()
            last_error = raw_stderr.replace(token, "***")

            if not is_transient_clone_error(raw_stderr):
                span.set_attribute("clone.attempts", attempt)
                span.set_attribute("clone.outcome", "permanent_failure")
                raise RuntimeError(f"git clone failed for {safe_url}: {last_error}")

            if attempt < max_retries:
                backoff = backoff_base * (3 ** (attempt - 1))
                await log.awarning(
                    "git_clone_retry",
                    attempt=attempt,
                    max_retries=max_retries,
                    error=last_error,
                    backoff_seconds=backoff,
                    url=safe_url,
                )
                await asyncio.sleep(backoff)

        span.set_attribute("clone.attempts", max_retries)
        span.set_attribute("clone.outcome", "transient_failure")
        raise TransientCloneError(
            f"git clone failed for {safe_url} after {max_retries} attempts: {last_error}",
            attempts=max_retries,
        )
