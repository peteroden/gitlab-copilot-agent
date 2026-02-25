"""Git CLI operations for branch creation, commits, and pushes."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import structlog

from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)

_GIT_TIMEOUT = 60
CLONE_DIR_PREFIX = "mr-review-"
MAX_PATCH_SIZE = 10 * 1024 * 1024  # 10 MB

# Patterns indicating transient clone errors worth retrying
_TRANSIENT_PATTERNS = [
    re.compile(r"The requested URL returned error: 403", re.IGNORECASE),
    re.compile(r"The requested URL returned error: 5\d{2}", re.IGNORECASE),
    re.compile(r"HTTP/\d[\d.]* 5\d{2}", re.IGNORECASE),
    re.compile(r"connection refused", re.IGNORECASE),
    re.compile(r"timed out", re.IGNORECASE),
    re.compile(r"Could not resolve host", re.IGNORECASE),
]

# Patterns indicating permanent errors that should NOT be retried
_PERMANENT_PATTERNS = [
    re.compile(r"repository not found", re.IGNORECASE),
    re.compile(r"not valid", re.IGNORECASE),
    re.compile(r"The requested URL returned error: 401", re.IGNORECASE),
    re.compile(r"The requested URL returned error: 404", re.IGNORECASE),
]


class TransientCloneError(RuntimeError):
    """Raised when git clone fails after exhausting retries on transient errors."""

    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


def _validate_clone_url(url: str) -> None:
    """Validate clone URL is HTTPS and has no embedded credentials.

    Raises:
        ValueError: If URL is invalid, not HTTPS, or contains credentials.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Invalid URL format: {e}") from e

    _allow_http = os.environ.get("ALLOW_HTTP_CLONE", "").lower() in ("true", "1", "yes")
    if parsed.scheme == "http" and _allow_http:
        pass  # E2E testing with mock git server
    elif parsed.scheme != "https":
        raise ValueError(f"Clone URL must use HTTPS scheme, got: {parsed.scheme}")

    if parsed.username or parsed.password:
        raise ValueError("Clone URL must not contain embedded credentials")

    if not parsed.netloc or not parsed.path:
        raise ValueError("Clone URL must have valid host and path")


def _sanitize_url_for_log(url: str) -> str:
    """Remove credentials from URL for safe logging."""
    try:
        parsed = urlparse(url)
        # If no scheme, it's not a valid URL
        if not parsed.scheme:
            return "<invalid-url>"
        if parsed.username or parsed.password:
            # Reconstruct URL without credentials
            netloc = parsed.hostname
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return f"{parsed.scheme}://{netloc}{parsed.path}"
        return url
    except Exception:
        return "<invalid-url>"


def _is_transient_clone_error(stderr: str) -> bool:
    """Return True if stderr indicates a transient (retryable) clone error."""
    if any(p.search(stderr) for p in _PERMANENT_PATTERNS):
        return False
    return any(p.search(stderr) for p in _TRANSIENT_PATTERNS)


async def _run_git(
    repo_path: Path,
    *args: str,
    sanitize_token: str | None = None,
    timeout: int = _GIT_TIMEOUT,
) -> str:
    """Run a git command and return stdout. Raises RuntimeError on failure."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_path),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s") from e

    if proc.returncode != 0:
        err = stderr.decode().strip()
        if sanitize_token:
            err = err.replace(sanitize_token, "***")
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {err}")

    return stdout.decode().strip()


async def git_create_branch(repo_path: Path, branch_name: str) -> None:
    """Create and checkout a new branch."""
    await _run_git(repo_path, "checkout", "-b", branch_name)
    await log.ainfo("branch_created", branch=branch_name, repo=str(repo_path))


async def git_unique_branch(repo_path: Path, base_name: str) -> str:
    """Create a branch with *base_name*, appending ``-2``, ``-3``, … on collision.

    Uses ``git ls-remote --heads`` to check remote branches (works with shallow
    clones).  Previous attempts are preserved for comparison.  Returns the
    actual branch name used.
    """
    raw = await _run_git(repo_path, "ls-remote", "--heads", "origin")
    remote_branches: set[str] = set()
    for line in raw.splitlines():
        ref = line.split("\t", 1)[-1] if "\t" in line else ""
        remote_branches.add(ref.removeprefix("refs/heads/"))

    candidate = base_name
    attempt = 1
    while candidate in remote_branches:
        attempt += 1
        candidate = f"{base_name}-{attempt}"
    await git_create_branch(repo_path, candidate)
    return candidate


async def git_commit(
    repo_path: Path,
    message: str,
    author_name: str,
    author_email: str,
) -> bool:
    """Stage all changes and commit. Returns False if nothing to commit."""
    await _run_git(repo_path, "add", ".")
    status = await _run_git(repo_path, "status", "--porcelain")
    if not status:
        await log.ainfo("nothing_to_commit", repo=str(repo_path))
        return False
    await _run_git(
        repo_path,
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit",
        "-m",
        message,
    )
    await log.ainfo("committed", commit_message=message, repo=str(repo_path))
    return True


async def git_push(
    repo_path: Path,
    remote: str,
    branch: str,
    token: str,
) -> None:
    """Push branch to remote with token sanitization in errors."""
    with _tracer.start_as_current_span("git.push", attributes={"branch": branch}):
        await _run_git(
            repo_path,
            "push",
            remote,
            "--",
            branch,
            sanitize_token=token,
        )
        await log.ainfo("pushed", branch=branch, remote=remote, repo=str(repo_path))


async def git_head_sha(repo_path: Path) -> str:
    """Return the HEAD commit SHA of a local repo."""
    return await _run_git(repo_path, "rev-parse", "HEAD")


async def git_diff_staged(repo_path: Path) -> str:
    """Return staged diff including binary files. Caller must `git add` first.

    Unlike other git helpers this reads stdout **without stripping** so that
    trailing context lines (blank lines rendered as a single space) are
    preserved — ``git apply`` requires the hunk to be complete.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_path),
        "diff",
        "--cached",
        "--binary",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise RuntimeError("git diff --cached timed out") from e
    if proc.returncode != 0:
        raise RuntimeError(f"git diff --cached failed: {stderr.decode().strip()}")
    return stdout.decode()


_PATH_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
# Match only actual file-header lines (not hunk content that happens to start with ---/+++)
_FILE_HEADER_RE = re.compile(r"^(diff --git |--- [ab]/|\+\+\+ [ab]/)")


def _validate_patch(patch: str) -> None:
    """Reject patches containing path traversal sequences in file headers."""
    for line in patch.splitlines():
        if _FILE_HEADER_RE.match(line) and _PATH_TRAVERSAL_RE.search(line):
            raise ValueError(f"Patch contains path traversal: {line!r}")


async def git_apply_patch(repo_path: Path, patch: str) -> None:
    """Apply a unified diff to *repo_path* using ``git apply --3way``.

    The patch is piped via stdin — no temp files on disk.
    Raises ValueError if the patch contains path traversal sequences.
    Raises RuntimeError if git apply fails.
    """
    _validate_patch(patch)
    with _tracer.start_as_current_span("git.apply_patch"):
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_path),
            "apply",
            "--whitespace=nowarn",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=patch.encode()), timeout=_GIT_TIMEOUT
            )
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise RuntimeError("git apply timed out") from e
        if proc.returncode != 0:
            raise RuntimeError(f"git apply failed: {stderr.decode().strip()}")
        await log.ainfo("patch_applied", repo=str(repo_path))


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
        _validate_clone_url(clone_url)
        safe_url = _sanitize_url_for_log(clone_url)

        auth_url = clone_url.replace("https://", f"https://oauth2:{token}@")
        _allow_http = os.environ.get("ALLOW_HTTP_CLONE", "").lower() in ("true", "1", "yes")
        clone_args = ["git", "clone"]
        if not _allow_http:
            clone_args.append("--depth=1")
        clone_args.extend(["--branch", branch, "--", auth_url])

        last_error = ""
        for attempt in range(1, max_retries + 1):
            tmp_dir = Path(tempfile.mkdtemp(prefix=CLONE_DIR_PREFIX, dir=clone_dir))
            # Append dest per attempt (fresh temp dir each time)
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
                # Timeouts are transient — retry
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

            if not _is_transient_clone_error(raw_stderr):
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
