"""Git CLI operations for branch creation, commits, and pushes."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import structlog

from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)

_GIT_TIMEOUT = 60
CLONE_DIR_PREFIX = "mr-review-"


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
) -> bool:
    """Stage all changes and commit. Returns False if nothing to commit."""
    await _run_git(repo_path, "add", ".")
    status = await _run_git(repo_path, "status", "--porcelain")
    if not status:
        await log.ainfo("nothing_to_commit", repo=str(repo_path))
        return False
    await _run_git(
        repo_path,
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
        "commit", "-m", message,
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
            repo_path, "push", remote, "--", branch,
            sanitize_token=token,
        )
        await log.ainfo("pushed", branch=branch, remote=remote, repo=str(repo_path))


async def git_clone(clone_url: str, branch: str, token: str) -> Path:
    """Clone repo to temp dir. Returns path."""
    with _tracer.start_as_current_span("git.clone", attributes={"branch": branch}):
        tmp_dir = Path(tempfile.mkdtemp(prefix=CLONE_DIR_PREFIX))
        auth_url = clone_url.replace("https://", f"https://oauth2:{token}@")
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth=1", "--branch", branch, "--", auth_url, str(tmp_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise RuntimeError("git clone timed out after 120s")
        if proc.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            sanitized = stderr.decode().strip().replace(token, "***")
            raise RuntimeError(f"git clone failed: {sanitized}")
        await log.ainfo("repo_cloned", path=str(tmp_dir), branch=branch)
        return tmp_dir
