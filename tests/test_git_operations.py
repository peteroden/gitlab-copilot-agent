"""Tests for git_operations — standalone async git helper functions."""

from pathlib import Path

import pytest

from gitlab_copilot_agent.git_operations import (
    TransientCloneError,
    _is_transient_clone_error,
    _sanitize_url_for_log,
    _validate_clone_url,
    git_commit,
    git_create_branch,
    git_push,
)
from tests.conftest import GITLAB_TOKEN

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
    """Test git_clone with URL validation and error sanitization."""

    async def test_successful_clone(self) -> None:
        """Clone should validate URL and return a path."""
        from unittest.mock import AsyncMock, patch

        from gitlab_copilot_agent.git_operations import git_clone

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        captured_args: tuple[str, ...] = ()

        async def mock_exec(*args: str, **kwargs: object) -> AsyncMock:
            nonlocal captured_args
            captured_args = args
            dest = Path(args[-1])
            if dest.exists():
                (dest / ".git").mkdir(parents=True, exist_ok=True)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            clone_path = await git_clone("https://gitlab.com/test/repo.git", "main", GITLAB_TOKEN)
            try:
                assert clone_path.exists()
                # Token should be in the auth URL arg, sanitized in errors
                assert any(f"oauth2:{GITLAB_TOKEN}@" in a for a in captured_args)
            finally:
                import shutil

                shutil.rmtree(clone_path, ignore_errors=True)

    async def test_clone_cleans_up_on_failure(self) -> None:
        """Clone directory should be removed when clone fails."""
        import tempfile

        from gitlab_copilot_agent.git_operations import git_clone

        temp_dir = tempfile.gettempdir()
        before = set(Path(temp_dir).glob("mr-review-*"))

        with pytest.raises(RuntimeError, match="git clone failed"):
            await git_clone("https://invalid.example.com/nonexistent.git", "main", GITLAB_TOKEN)

        after = set(Path(temp_dir).glob("mr-review-*"))
        assert after - before == set(), "clone directories should be cleaned up on failure"

    async def test_sanitizes_token_in_clone_errors(self) -> None:
        """Token must not appear in clone error messages."""
        from unittest.mock import AsyncMock, patch

        from gitlab_copilot_agent.git_operations import git_clone

        secret = "glpat-super-secret"
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"",
            f"fatal: could not read from https://oauth2:{secret}@gitlab.com/x.git".encode(),
        )
        mock_proc.returncode = 128

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            pytest.raises(RuntimeError) as exc_info,
        ):
            await git_clone("https://gitlab.com/x.git", "main", secret)

        assert secret not in str(exc_info.value)
        assert "***" in str(exc_info.value)

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

    def test_allows_http_when_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP URLs allowed when ALLOW_HTTP_CLONE is set (E2E testing)."""
        monkeypatch.setenv("ALLOW_HTTP_CLONE", "true")
        _validate_clone_url("http://localhost:9999/repo.git")

    def test_rejects_http_when_env_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ALLOW_HTTP_CLONE=false must not enable HTTP cloning."""
        monkeypatch.setenv("ALLOW_HTTP_CLONE", "false")
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


class TestIsTransientCloneError:
    """Test transient vs permanent error classification."""

    def test_403_is_transient(self) -> None:
        assert _is_transient_clone_error("The requested URL returned error: 403")

    def test_500_is_transient(self) -> None:
        assert _is_transient_clone_error("The requested URL returned error: 503")

    def test_connection_refused_is_transient(self) -> None:
        assert _is_transient_clone_error("fatal: unable to access: connection refused")

    def test_timed_out_is_transient(self) -> None:
        assert _is_transient_clone_error("fatal: timed out")

    def test_could_not_resolve_host_is_transient(self) -> None:
        assert _is_transient_clone_error("Could not resolve host: gitlab.example.com")

    def test_401_is_permanent(self) -> None:
        assert not _is_transient_clone_error("The requested URL returned error: 401")

    def test_404_is_permanent(self) -> None:
        assert not _is_transient_clone_error("The requested URL returned error: 404")

    def test_repo_not_found_is_permanent(self) -> None:
        assert not _is_transient_clone_error("repository not found")

    def test_unrecognized_error_is_not_transient(self) -> None:
        assert not _is_transient_clone_error("some unknown error")


class TestGitCloneRetry:
    """Test retry behavior in git_clone."""

    async def test_retries_on_transient_error_then_succeeds(self) -> None:
        """Clone should retry on transient errors and succeed when resolved."""
        from unittest.mock import AsyncMock, patch

        from gitlab_copilot_agent.git_operations import git_clone

        call_count = 0

        async def mock_exec(*args: str, **kwargs: object) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            proc = AsyncMock()
            if call_count == 1:
                proc.communicate.return_value = (
                    b"",
                    b"The requested URL returned error: 403",
                )
                proc.returncode = 128
            else:
                dest = Path(args[-1])
                if dest.exists():
                    (dest / ".git").mkdir(parents=True, exist_ok=True)
                proc.communicate.return_value = (b"", b"")
                proc.returncode = 0
            return proc

        with (
            patch("asyncio.create_subprocess_exec", side_effect=mock_exec),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await git_clone(
                "https://gitlab.com/test/repo.git",
                "main",
                GITLAB_TOKEN,
                max_retries=3,
                backoff_base=0.01,
            )
            try:
                assert result.exists()
                assert call_count == 2
                mock_sleep.assert_awaited_once()
            finally:
                import shutil

                shutil.rmtree(result, ignore_errors=True)

    async def test_exhausts_retries_raises_transient_clone_error(self) -> None:
        """After max retries, TransientCloneError should be raised."""
        from unittest.mock import AsyncMock, patch

        from gitlab_copilot_agent.git_operations import git_clone

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"",
            b"The requested URL returned error: 503",
        )
        mock_proc.returncode = 128

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(TransientCloneError) as exc_info:
                await git_clone(
                    "https://gitlab.com/test/repo.git",
                    "main",
                    GITLAB_TOKEN,
                    max_retries=2,
                    backoff_base=0.01,
                )
            assert exc_info.value.attempts == 2
            assert "2 attempts" in str(exc_info.value)

    async def test_permanent_error_fails_immediately(self) -> None:
        """Non-transient errors should fail immediately without retry."""
        from unittest.mock import AsyncMock, patch

        from gitlab_copilot_agent.git_operations import git_clone

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"",
            b"repository not found",
        )
        mock_proc.returncode = 128

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            with pytest.raises(RuntimeError, match="repository not found"):
                await git_clone(
                    "https://gitlab.com/test/repo.git",
                    "main",
                    GITLAB_TOKEN,
                    max_retries=3,
                    backoff_base=0.01,
                )
            mock_sleep.assert_not_awaited()
