"""Git CLI operations for branch creation, commits, and pushes."""

from __future__ import annotations

import asyncio
import os
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


def _validate_clone_url(url: str) -> None:
    """Validate clone URL is HTTPS and has no embedded credentials.

    Raises:
        ValueError: If URL is invalid, not HTTPS, or contains credentials.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Invalid URL format: {e}") from e

    if parsed.scheme != "https":
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
    await log.ainfo("committed", message=message, repo=str(repo_path))
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


async def git_clone(clone_url: str, branch: str, token: str) -> Path:
    """Clone repo to temp dir using secure credential passing. Returns path.

    Args:
        clone_url: HTTPS URL to clone (must not contain embedded credentials)
        branch: Branch name to checkout
        token: GitLab token for authentication

    Raises:
        ValueError: If clone_url is invalid or contains embedded credentials
        RuntimeError: If git clone fails
    """
    with _tracer.start_as_current_span("git.clone", attributes={"branch": branch}):
        # Validate URL before any git operations
        _validate_clone_url(clone_url)

        # Create askpass script in a separate temp directory to avoid polluting clone destination
        askpass_dir = Path(tempfile.mkdtemp(prefix="git-askpass-"))
        askpass_script = askpass_dir / ".git-askpass.sh"
        clone_dest: Path | None = None

        try:
            # Create a temporary askpass script to provide credentials securely
            # This avoids putting the token in the URL or command line args
            askpass_script.write_text(f'#!/bin/sh\necho "{token}"\n')
            askpass_script.chmod(0o700)

            # Set up environment to use askpass for credentials
            env = os.environ.copy()
            env["GIT_ASKPASS"] = str(askpass_script)
            env["GIT_USERNAME"] = "oauth2"
            # Disable terminal prompts to ensure askpass is used
            env["GIT_TERMINAL_PROMPT"] = "0"

            # Create destination directory for clone
            clone_dest = Path(tempfile.mkdtemp(prefix=CLONE_DIR_PREFIX))

            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth=1",
                "--branch",
                branch,
                "--",
                clone_url,
                str(clone_dest),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except TimeoutError as e:
                proc.kill()
                raise RuntimeError("git clone timed out after 120s") from e

            if proc.returncode != 0:
                # Sanitize any URLs or tokens that might appear in error output
                err_msg = stderr.decode().strip()
                # Replace token if it somehow appears
                err_msg = err_msg.replace(token, "***")
                # Sanitize any URLs with credentials
                safe_url = _sanitize_url_for_log(clone_url)
                raise RuntimeError(f"git clone failed for {safe_url}: {err_msg}")

            await log.ainfo("repo_cloned", path=str(clone_dest), branch=branch)
            return clone_dest

        except Exception:
            # Clean up clone destination on any error
            if clone_dest is not None:
                shutil.rmtree(clone_dest, ignore_errors=True)
            raise
        finally:
            # Always clean up askpass artifacts
            shutil.rmtree(askpass_dir, ignore_errors=True)
