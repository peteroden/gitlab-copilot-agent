"""Tests for git_operations — standalone async git helper functions."""

import asyncio
from pathlib import Path

import pytest

from gitlab_copilot_agent.git_operations import (
    git_commit,
    git_create_branch,
    git_push,
)

AUTHOR_NAME = "Test Agent"
AUTHOR_EMAIL = "agent@test.com"


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """Create a bare git repo with one commit to serve as remote."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _run_sync("git", "init", "--bare", "--initial-branch=main", str(bare))
    return bare


@pytest.fixture
def work_repo(tmp_path: Path, bare_repo: Path) -> Path:
    """Clone the bare repo into a working directory with an initial commit."""
    work = tmp_path / "work"
    _run_sync("git", "clone", str(bare_repo), str(work))
    _run_sync("git", "-C", str(work), "checkout", "-b", "main")
    # Create initial commit so branches can be created
    (work / "README.md").write_text("init")
    _run_sync("git", "-C", str(work), "add", ".")
    _run_sync(
        "git", "-C", str(work),
        "-c", f"user.name={AUTHOR_NAME}",
        "-c", f"user.email={AUTHOR_EMAIL}",
        "commit", "-m", "initial",
    )
    _run_sync("git", "-C", str(work), "push", "-u", "origin", "main")
    return work


def _run_sync(*args: str) -> str:
    """Run a subprocess synchronously for test setup."""
    import subprocess

    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class TestGitCreateBranch:
    async def test_creates_and_checks_out_branch(self, work_repo: Path) -> None:
        await git_create_branch(work_repo, "feature/test")

        current = _run_sync("git", "-C", str(work_repo), "branch", "--show-current")
        assert current == "feature/test"

    async def test_branch_with_slash_prefix(self, work_repo: Path) -> None:
        await git_create_branch(work_repo, "agent/PROJ-123/add-login")

        current = _run_sync("git", "-C", str(work_repo), "branch", "--show-current")
        assert current == "agent/PROJ-123/add-login"

    async def test_fails_on_existing_branch(self, work_repo: Path) -> None:
        await git_create_branch(work_repo, "dupe")

        _run_sync("git", "-C", str(work_repo), "checkout", "main")
        with pytest.raises(RuntimeError, match="git checkout .* failed"):
            await git_create_branch(work_repo, "dupe")


class TestGitCommit:
    async def test_stages_and_commits_changes(self, work_repo: Path) -> None:
        (work_repo / "new_file.py").write_text("print('hello')")

        await git_commit(work_repo, "feat: add new file", AUTHOR_NAME, AUTHOR_EMAIL)

        log_out = _run_sync("git", "-C", str(work_repo), "log", "--oneline", "-1")
        assert "feat: add new file" in log_out

    async def test_commit_author(self, work_repo: Path) -> None:
        (work_repo / "another.py").write_text("x = 1")

        await git_commit(work_repo, "test commit", AUTHOR_NAME, AUTHOR_EMAIL)

        author = _run_sync(
            "git", "-C", str(work_repo), "log", "-1", "--format=%an <%ae>"
        )
        assert author == f"{AUTHOR_NAME} <{AUTHOR_EMAIL}>"

    async def test_returns_false_with_nothing_to_commit(self, work_repo: Path) -> None:
        result = await git_commit(work_repo, "empty", AUTHOR_NAME, AUTHOR_EMAIL)
        assert result is False


class TestGitPush:
    async def test_pushes_branch_to_remote(
        self, work_repo: Path, bare_repo: Path
    ) -> None:
        await git_create_branch(work_repo, "feature/push-test")
        (work_repo / "pushed.txt").write_text("data")
        await git_commit(work_repo, "feat: push test", AUTHOR_NAME, AUTHOR_EMAIL)

        await git_push(work_repo, "origin", "feature/push-test", token="fake-token")

        # Verify branch exists in bare repo
        branches = _run_sync("git", "-C", str(bare_repo), "branch")
        assert "feature/push-test" in branches

    async def test_sanitizes_token_in_errors(self, work_repo: Path) -> None:
        """Verify token is sanitized if it appears in git error output."""
        from unittest.mock import AsyncMock, patch

        secret = "glpat-super-secret-token"
        # Simulate git returning an error containing the token
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", f"fatal: auth failed for https://oauth2:{secret}@gitlab.com/repo.git".encode())
        mock_proc.returncode = 1
        mock_proc.kill = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError) as exc_info:
                await git_push(work_repo, "origin", "main", token=secret)

        assert secret not in str(exc_info.value)
        assert "***" in str(exc_info.value)


class TestRunGitTimeout:
    async def test_timeout_raises_runtime_error(self, work_repo: Path) -> None:
        from gitlab_copilot_agent.git_operations import _run_git

        # Use a git command that takes time — hash-object with stdin that never closes
        with pytest.raises(RuntimeError, match="timed out"):
            await _run_git(work_repo, "gc", "--aggressive", timeout=0)
