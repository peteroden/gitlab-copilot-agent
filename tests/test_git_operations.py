"""Tests for git_operations — standalone async git helper functions."""

from pathlib import Path

import pytest

from gitlab_copilot_agent.git_operations import (
    _sanitize_url_for_log,
    _validate_clone_url,
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
        "git",
        "-C",
        str(work),
        "-c",
        f"user.name={AUTHOR_NAME}",
        "-c",
        f"user.email={AUTHOR_EMAIL}",
        "commit",
        "-m",
        "initial",
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

        author = _run_sync("git", "-C", str(work_repo), "log", "-1", "--format=%an <%ae>")
        assert author == f"{AUTHOR_NAME} <{AUTHOR_EMAIL}>"

    async def test_returns_false_with_nothing_to_commit(self, work_repo: Path) -> None:
        result = await git_commit(work_repo, "empty", AUTHOR_NAME, AUTHOR_EMAIL)
        assert result is False


class TestGitPush:
    async def test_pushes_branch_to_remote(self, work_repo: Path, bare_repo: Path) -> None:
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
        mock_proc.communicate.return_value = (
            b"",
            f"fatal: auth failed for https://oauth2:{secret}@gitlab.com/repo.git".encode(),
        )
        mock_proc.returncode = 1
        mock_proc.kill = AsyncMock()

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            pytest.raises(RuntimeError) as exc_info,
        ):
            await git_push(work_repo, "origin", "main", token=secret)

        assert secret not in str(exc_info.value)
        assert "***" in str(exc_info.value)


class TestRunGitTimeout:
    async def test_timeout_raises_runtime_error(self, work_repo: Path) -> None:
        from gitlab_copilot_agent.git_operations import _run_git

        # Use a git command that takes time — hash-object with stdin that never closes
        with pytest.raises(RuntimeError, match="timed out"):
            await _run_git(work_repo, "gc", "--aggressive", timeout=0)


class TestGitClone:
    """Test git_clone function with URL validation and secure credential handling."""

    async def test_successful_clone_with_secure_credentials(self, bare_repo: Path) -> None:
        """Clone should successfully clone a valid repo using askpass for credentials."""
        from unittest.mock import AsyncMock

        from gitlab_copilot_agent.git_operations import git_clone

        # Mock the subprocess to simulate successful clone
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        import asyncio
        from unittest.mock import patch

        clone_path = None
        askpass_dir_created = None
        clone_dest_created = None
        clone_dest_was_empty = False

        original_create_subprocess_exec = asyncio.create_subprocess_exec

        async def mock_create_subprocess_exec(*args, **kwargs):
            nonlocal clone_dest_was_empty, clone_dest_created
            # Capture the clone destination from args
            if len(args) >= 2 and args[0] == "git" and args[1] == "clone":
                clone_dest_created = Path(args[-1])
                # Check if destination is empty when clone is called
                if clone_dest_created.exists():
                    clone_dest_was_empty = len(list(clone_dest_created.iterdir())) == 0
                # Simulate git clone creating .git directory
                (clone_dest_created / ".git").mkdir(parents=True, exist_ok=True)
                (clone_dest_created / "README.md").write_text("init")
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec):
            clone_path = await git_clone("https://gitlab.com/test/repo.git", "main", "test-token")

        try:
            # Verify clone path was returned
            assert clone_path is not None
            assert clone_path.exists()
            assert (clone_path / ".git").is_dir()

            # Verify destination was empty when git clone was called
            assert clone_dest_was_empty, "Clone destination must be empty when git clone executes"

            # Verify askpass script was cleaned up (not in clone dir)
            askpass_files = list(clone_path.glob("**/.git-askpass.sh"))
            assert len(askpass_files) == 0, "askpass script should not be in clone directory"

            # Verify no askpass temp dirs left behind
            import tempfile

            temp_dir = Path(tempfile.gettempdir())
            askpass_dirs = list(temp_dir.glob("git-askpass-*"))
            assert len(askpass_dirs) == 0, "askpass temp directories should be cleaned up"
        finally:
            # Clean up clone directory
            import shutil

            if clone_path:
                shutil.rmtree(clone_path, ignore_errors=True)

    async def test_clone_cleans_up_askpass_on_failure(self) -> None:
        """Clone should clean up askpass artifacts even when clone fails."""
        from gitlab_copilot_agent.git_operations import git_clone

        # Count temp directories before clone attempt
        import tempfile

        temp_dir = tempfile.gettempdir()
        before_askpass_dirs = list(Path(temp_dir).glob("git-askpass-*"))
        before_clone_dirs = list(Path(temp_dir).glob("mr-review-*"))

        # Attempt to clone a non-existent repo (will fail)
        with pytest.raises(RuntimeError, match="git clone failed"):
            await git_clone("https://invalid.example.com/nonexistent.git", "main", "test-token")

        # Verify no new askpass directories were left behind
        after_askpass_dirs = list(Path(temp_dir).glob("git-askpass-*"))
        new_askpass_dirs = set(after_askpass_dirs) - set(before_askpass_dirs)
        assert len(new_askpass_dirs) == 0, "askpass directories should be cleaned up on failure"

        # Verify no new clone directories were left behind
        after_clone_dirs = list(Path(temp_dir).glob("mr-review-*"))
        new_clone_dirs = set(after_clone_dirs) - set(before_clone_dirs)
        assert len(new_clone_dirs) == 0, "clone directories should be cleaned up on failure"

    async def test_clone_destination_is_initially_empty(self) -> None:
        """Regression test: clone destination must be empty when git clone runs.
        
        This test specifically catches the bug where askpass script was created
        in the clone destination directory, causing git clone to fail.
        """
        from unittest.mock import AsyncMock, patch

        from gitlab_copilot_agent.git_operations import git_clone

        clone_dest_was_empty_at_call_time = False
        askpass_in_clone_dest = False

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        import asyncio

        original_create_subprocess_exec = asyncio.create_subprocess_exec

        async def mock_create_subprocess_exec(*args, **kwargs):
            nonlocal clone_dest_was_empty_at_call_time, askpass_in_clone_dest
            # Check if this is the git clone command
            if len(args) >= 2 and args[0] == "git" and args[1] == "clone":
                # Find the destination directory argument (last arg)
                dest_dir = Path(args[-1])

                # Verify destination is empty (or doesn't exist yet)
                if dest_dir.exists():
                    contents = list(dest_dir.iterdir())
                    clone_dest_was_empty_at_call_time = len(contents) == 0
                    # Check if any askpass files are in the destination
                    askpass_in_clone_dest = any(".git-askpass" in str(f) for f in contents)
                else:
                    clone_dest_was_empty_at_call_time = True

                # Simulate successful clone
                (dest_dir / ".git").mkdir(parents=True, exist_ok=True)

            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec):
            clone_path = await git_clone("https://gitlab.com/test/repo.git", "main", "test-token")
            try:
                assert clone_dest_was_empty_at_call_time, (
                    "git clone destination directory must be empty when clone executes"
                )
                assert not askpass_in_clone_dest, (
                    "askpass script must not be in clone destination directory"
                )
            finally:
                import shutil

                shutil.rmtree(clone_path, ignore_errors=True)

    async def test_rejects_url_with_embedded_credentials(self) -> None:
        """Clone should reject URLs with embedded credentials."""
        from gitlab_copilot_agent.git_operations import git_clone

        with pytest.raises(ValueError, match="must not contain embedded credentials"):
            await git_clone("https://oauth2:token@gitlab.com/project.git", "main", "fake-token")

    async def test_rejects_http_url(self) -> None:
        """Clone should reject non-HTTPS URLs."""
        from gitlab_copilot_agent.git_operations import git_clone

        with pytest.raises(ValueError, match="must use HTTPS scheme"):
            await git_clone("http://gitlab.com/project.git", "main", "fake-token")


class TestValidateCloneUrl:
    """Test URL validation for secure clone operations."""

    def test_valid_https_url(self) -> None:
        """Valid HTTPS URL should pass validation."""
        _validate_clone_url("https://gitlab.com/group/project.git")
        _validate_clone_url("https://gitlab.example.com:8443/my/repo.git")

    def test_rejects_http_scheme(self) -> None:
        """HTTP URLs must be rejected."""
        with pytest.raises(ValueError, match="must use HTTPS scheme"):
            _validate_clone_url("http://gitlab.com/project.git")

    def test_rejects_ssh_scheme(self) -> None:
        """SSH URLs must be rejected."""
        with pytest.raises(ValueError, match="must use HTTPS scheme"):
            _validate_clone_url("git@gitlab.com:group/project.git")

    def test_rejects_embedded_username(self) -> None:
        """URLs with embedded username must be rejected."""
        with pytest.raises(ValueError, match="must not contain embedded credentials"):
            _validate_clone_url("https://user@gitlab.com/project.git")

    def test_rejects_embedded_password(self) -> None:
        """URLs with embedded password must be rejected."""
        with pytest.raises(ValueError, match="must not contain embedded credentials"):
            _validate_clone_url("https://user:pass@gitlab.com/project.git")

    def test_rejects_embedded_token(self) -> None:
        """URLs with embedded token must be rejected."""
        with pytest.raises(ValueError, match="must not contain embedded credentials"):
            _validate_clone_url("https://oauth2:glpat-abc123@gitlab.com/project.git")

    def test_rejects_malformed_url(self) -> None:
        """Malformed URLs must be rejected."""
        with pytest.raises(ValueError, match="must have valid host and path"):
            _validate_clone_url("https://")

    def test_rejects_empty_path(self) -> None:
        """URLs without path must be rejected."""
        with pytest.raises(ValueError, match="must have valid host and path"):
            _validate_clone_url("https://gitlab.com")


class TestSanitizeUrlForLog:
    """Test URL sanitization for safe logging."""

    def test_clean_url_unchanged(self) -> None:
        """URLs without credentials should pass through unchanged."""
        url = "https://gitlab.com/group/project.git"
        assert _sanitize_url_for_log(url) == url

    def test_removes_username(self) -> None:
        """Username should be removed from URL."""
        url = "https://user@gitlab.com/project.git"
        sanitized = _sanitize_url_for_log(url)
        assert "user" not in sanitized
        assert "gitlab.com/project.git" in sanitized

    def test_removes_password(self) -> None:
        """Username and password should be removed from URL."""
        url = "https://user:secret@gitlab.com/project.git"
        sanitized = _sanitize_url_for_log(url)
        assert "user" not in sanitized
        assert "secret" not in sanitized
        assert "gitlab.com/project.git" in sanitized

    def test_removes_token(self) -> None:
        """OAuth token should be removed from URL."""
        url = "https://oauth2:glpat-secret-token@gitlab.com/project.git"
        sanitized = _sanitize_url_for_log(url)
        assert "oauth2" not in sanitized
        assert "glpat-secret-token" not in sanitized
        assert "gitlab.com/project.git" in sanitized

    def test_preserves_port(self) -> None:
        """Port number should be preserved after credential removal."""
        url = "https://user:pass@gitlab.com:8443/project.git"
        sanitized = _sanitize_url_for_log(url)
        assert "8443" in sanitized
        assert "user" not in sanitized
        assert "pass" not in sanitized

    def test_handles_invalid_url(self) -> None:
        """Invalid URLs should return placeholder."""
        assert _sanitize_url_for_log("not-a-url") == "<invalid-url>"
