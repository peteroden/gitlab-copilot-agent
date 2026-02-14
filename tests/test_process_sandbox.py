"""Tests for process_sandbox module."""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from gitlab_copilot_agent.process_sandbox import (
    BubblewrapSandbox,
    NoopSandbox,
    _get_real_cli_path,
    get_sandbox,
)


class TestGetRealCliPath:
    """Tests for _get_real_cli_path helper."""

    def test_returns_path(self) -> None:
        """Should return a valid path to the copilot CLI."""
        path = _get_real_cli_path()
        assert os.path.exists(path)
        assert path.endswith("copilot")

    def test_raises_if_not_found(self) -> None:
        """Should raise RuntimeError if CLI not found."""
        with patch("gitlab_copilot_agent.process_sandbox._copilot_pkg") as mock_pkg:
            mock_pkg.__file__ = "/nonexistent/copilot/__init__.py"
            with pytest.raises(RuntimeError, match="not found"):
                _get_real_cli_path()


class TestBubblewrapSandbox:
    """Tests for BubblewrapSandbox implementation."""

    def test_creates_executable_script(self, tmp_path: Path) -> None:
        """Should create an executable wrapper script."""
        sandbox = BubblewrapSandbox()
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        path = sandbox.create_cli_wrapper(repo)
        try:
            assert os.path.exists(path)
            assert os.stat(path).st_mode & stat.S_IXUSR  # executable
        finally:
            sandbox.cleanup()

    def test_script_contains_bwrap(self, tmp_path: Path) -> None:
        """Should generate a script with correct bwrap arguments."""
        sandbox = BubblewrapSandbox()
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        path = sandbox.create_cli_wrapper(repo)
        try:
            with open(path) as f:
                content = f.read()
            assert "bwrap" in content
            assert "--ro-bind /usr /usr" in content
            assert "--tmpfs /tmp" in content
            assert "--tmpfs /home" in content
            assert f"--bind {repo}" in content or f"--bind '{repo}'" in content
            assert "--unshare-all" in content
            assert "--share-net" in content
            assert "--die-with-parent" in content
            assert '"$@"' in content
        finally:
            sandbox.cleanup()

    def test_script_quotes_paths_with_spaces(self, tmp_path: Path) -> None:
        """Should properly quote paths containing spaces."""
        sandbox = BubblewrapSandbox()
        repo = str(tmp_path / "repo with spaces")
        os.makedirs(repo)
        path = sandbox.create_cli_wrapper(repo)
        try:
            with open(path) as f:
                content = f.read()
            # shlex.quote wraps in single quotes for paths with spaces
            assert "'" in content  # Path should be quoted
        finally:
            sandbox.cleanup()

    def test_cleanup_removes_script(self, tmp_path: Path) -> None:
        """Should remove the wrapper script on cleanup."""
        sandbox = BubblewrapSandbox()
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        path = sandbox.create_cli_wrapper(repo)
        assert os.path.exists(path)
        sandbox.cleanup()
        assert not os.path.exists(path)

    def test_cleanup_safe_when_no_script(self) -> None:
        """Should not raise when cleanup called without creating script."""
        sandbox = BubblewrapSandbox()
        sandbox.cleanup()  # Should not raise

    def test_cleanup_safe_when_already_deleted(self, tmp_path: Path) -> None:
        """Should not raise if script already deleted."""
        sandbox = BubblewrapSandbox()
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        path = sandbox.create_cli_wrapper(repo)
        os.unlink(path)
        sandbox.cleanup()  # Should not raise


class TestNoopSandbox:
    """Tests for NoopSandbox fallback implementation."""

    def test_returns_real_cli_path(self) -> None:
        """Should return the real CLI path without sandboxing."""
        sandbox = NoopSandbox()
        path = sandbox.create_cli_wrapper("/some/repo")
        assert path.endswith("copilot")
        assert os.path.exists(path)

    def test_cleanup_is_noop(self) -> None:
        """Should not raise on cleanup."""
        sandbox = NoopSandbox()
        sandbox.cleanup()  # Should not raise


class TestGetSandbox:
    """Tests for get_sandbox factory function."""

    def test_returns_bubblewrap_when_available(self) -> None:
        """Should return BubblewrapSandbox when bwrap is on PATH."""
        with patch(
            "gitlab_copilot_agent.process_sandbox.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            sandbox = get_sandbox()
            assert isinstance(sandbox, BubblewrapSandbox)

    def test_returns_noop_when_not_available(self) -> None:
        """Should return NoopSandbox when bwrap is not found."""
        with patch("gitlab_copilot_agent.process_sandbox.shutil.which", return_value=None):
            sandbox = get_sandbox()
            assert isinstance(sandbox, NoopSandbox)
