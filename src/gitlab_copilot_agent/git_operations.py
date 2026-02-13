"""Git CLI operations for branch creation, commits, and pushes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

log = structlog.get_logger()

_GIT_TIMEOUT = 60


async def _run_git(
    repo_path: Path,
    *args: str,
    sanitize_token: str | None = None,
    timeout: int = _GIT_TIMEOUT,
) -> str:
    """Run a git command and return stdout. Raises RuntimeError on failure."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo_path), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s")

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
) -> None:
    """Stage all changes and commit."""
    await _run_git(repo_path, "add", ".")
    await _run_git(
        repo_path,
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
        "commit", "-m", message,
    )
    await log.ainfo("committed", message=message, repo=str(repo_path))


async def git_push(
    repo_path: Path,
    remote: str,
    branch: str,
    token: str,
) -> None:
    """Push branch to remote with token sanitization in errors."""
    await _run_git(
        repo_path, "push", remote, "--", branch,
        sanitize_token=token,
    )
    await log.ainfo("pushed", branch=branch, remote=remote, repo=str(repo_path))
