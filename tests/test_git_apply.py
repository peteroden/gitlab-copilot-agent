"""Tests for git_apply_patch, git_head_sha, git_diff_staged, and _validate_patch."""

import asyncio
from pathlib import Path

import pytest

from gitlab_copilot_agent.git_operations import (
    _validate_patch,
    git_apply_patch,
    git_diff_staged,
    git_head_sha,
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one commit."""

    async def _init() -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()

        async def _run(*args: str) -> None:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(repo),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        await _run("init")
        await _run("config", "user.email", "test@test.com")
        await _run("config", "user.name", "Test")
        (repo / "file.txt").write_text("hello\n")
        await _run("add", ".")
        await _run("commit", "-m", "init")
        return repo

    return asyncio.get_event_loop().run_until_complete(_init())


class TestGitHeadSha:
    async def test_returns_sha(self, git_repo: Path) -> None:
        sha = await git_head_sha(git_repo)
        assert len(sha) == 40
        assert sha.isalnum()


class TestGitDiffStaged:
    async def test_empty_when_no_changes(self, git_repo: Path) -> None:
        diff = await git_diff_staged(git_repo)
        assert diff == ""

    async def test_returns_diff_after_add(self, git_repo: Path) -> None:
        (git_repo / "new.txt").write_text("new content\n")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(git_repo),
            "add",
            ".",
            stdout=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        diff = await git_diff_staged(git_repo)
        assert "new.txt" in diff
        assert "+new content" in diff


class TestValidatePatch:
    def test_clean_patch_ok(self) -> None:
        patch = "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
        _validate_patch(patch)  # should not raise

    def test_traversal_in_diff_header(self) -> None:
        patch = "diff --git a/../etc/passwd b/../etc/passwd\n"
        with pytest.raises(ValueError, match="path traversal"):
            _validate_patch(patch)

    def test_traversal_in_minus_line(self) -> None:
        patch = "--- a/../secret\n"
        with pytest.raises(ValueError, match="path traversal"):
            _validate_patch(patch)

    def test_traversal_in_plus_line(self) -> None:
        patch = "+++ b/../secret\n"
        with pytest.raises(ValueError, match="path traversal"):
            _validate_patch(patch)

    def test_dotdot_in_content_lines_ok(self) -> None:
        patch = "+path = ../../something\n-old/../path\n"
        _validate_patch(patch)  # content lines aren't checked

    def test_hunk_content_with_triple_plus_ok(self) -> None:
        # Content lines starting with +++ shouldn't trigger false positive
        patch = "+++../../new\n---../../old\n"
        _validate_patch(patch)  # no a/ or b/ prefix = not a file header


class TestGitApplyPatch:
    async def test_applies_valid_patch(self, git_repo: Path) -> None:
        # Generate a patch from changes
        (git_repo / "file.txt").write_text("modified\n")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(git_repo),
            "add",
            ".",
            stdout=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        diff = await git_diff_staged(git_repo)

        # Reset index and working tree to clean state
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(git_repo),
            "reset",
            "HEAD",
            "--",
            ".",
            stdout=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(git_repo),
            "checkout",
            "--",
            ".",
            stdout=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        assert (git_repo / "file.txt").read_text() == "hello\n"

        # Apply the patch
        await git_apply_patch(git_repo, diff)
        assert (git_repo / "file.txt").read_text() == "modified\n"

    async def test_rejects_traversal_patch(self, git_repo: Path) -> None:
        bad_patch = "diff --git a/../etc/passwd b/../etc/passwd\n"
        with pytest.raises(ValueError, match="path traversal"):
            await git_apply_patch(git_repo, bad_patch)

    async def test_raises_on_bad_patch(self, git_repo: Path) -> None:
        with pytest.raises(RuntimeError, match="git apply failed"):
            await git_apply_patch(git_repo, "not a valid patch\n")
