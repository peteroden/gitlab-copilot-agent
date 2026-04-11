"""Core git CLI runner and branch/commit/push operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.telemetry import get_tracer

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger()
_tracer = get_tracer(__name__)

_GIT_TIMEOUT = 60


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
