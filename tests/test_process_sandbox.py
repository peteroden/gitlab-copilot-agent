"""Tests for process_sandbox module."""

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gitlab_copilot_agent.process_sandbox import (
    BubblewrapSandbox,
    ContainerSandbox,
    NoopSandbox,
    _get_real_cli_path,
    get_sandbox,
)
from tests.conftest import make_settings


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

    def test_preflight_succeeds_when_bwrap_available(self) -> None:
        """Should pass preflight when bwrap is on PATH and functional."""
        with (
            patch("shutil.which", return_value="/usr/bin/bwrap"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            sandbox = BubblewrapSandbox()
            sandbox.preflight()  # Should not raise

    def test_preflight_raises_when_bwrap_missing(self) -> None:
        """Should raise RuntimeError when bwrap not found."""
        with patch("shutil.which", return_value=None):
            sandbox = BubblewrapSandbox()
            with pytest.raises(RuntimeError, match="bwrap binary not found"):
                sandbox.preflight()

    def test_preflight_raises_when_bwrap_fails(self) -> None:
        """Should raise RuntimeError when bwrap --version fails."""
        with (
            patch("shutil.which", return_value="/usr/bin/bwrap"),
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "bwrap")),
        ):
            sandbox = BubblewrapSandbox()
            with pytest.raises(RuntimeError, match="bwrap preflight check failed"):
                sandbox.preflight()

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

    def test_preflight_always_passes(self) -> None:
        """Should always pass preflight check."""
        sandbox = NoopSandbox()
        sandbox.preflight()  # Should not raise

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


_TEST_SANDBOX_IMAGE = "copilot-cli-sandbox:test"


class TestContainerSandbox:
    """Tests for ContainerSandbox implementation."""

    def test_preflight_succeeds(self) -> None:
        """Should pass preflight when runtime is available and image exists."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run") as mock_run,
        ):
            # First call is docker info, second is image inspect
            mock_run.side_effect = [
                MagicMock(returncode=0),  # docker info
                MagicMock(returncode=0),  # docker image inspect
            ]
            sandbox = ContainerSandbox(runtime="docker", image=_TEST_SANDBOX_IMAGE)
            sandbox.preflight()  # Should not raise

    def test_preflight_raises_when_runtime_missing(self) -> None:
        """Should raise RuntimeError when runtime not found on PATH."""
        with patch("shutil.which", return_value=None):
            sandbox = ContainerSandbox(runtime="docker", image=_TEST_SANDBOX_IMAGE)
            with pytest.raises(RuntimeError, match="docker binary not found"):
                sandbox.preflight()

    def test_preflight_raises_when_runtime_fails(self) -> None:
        """Should raise RuntimeError when runtime info command fails."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "docker")),
        ):
            sandbox = ContainerSandbox(runtime="docker", image=_TEST_SANDBOX_IMAGE)
            with pytest.raises(RuntimeError, match="docker preflight failed"):
                sandbox.preflight()

    def test_preflight_raises_when_image_missing(self) -> None:
        """Should raise RuntimeError when sandbox image not found."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run") as mock_run,
        ):
            # docker info succeeds, but image inspect fails
            mock_run.side_effect = [
                MagicMock(returncode=0),  # docker info
                MagicMock(returncode=1),  # docker image inspect (not found)
            ]
            sandbox = ContainerSandbox(runtime="docker", image=_TEST_SANDBOX_IMAGE)
            with pytest.raises(RuntimeError, match="Sandbox image .* not found"):
                sandbox.preflight()

    def test_create_cli_wrapper_generates_executable_script(self, tmp_path: Path) -> None:
        """Should generate an executable script that invokes the container runtime."""
        sandbox = ContainerSandbox(runtime="docker", image=_TEST_SANDBOX_IMAGE)
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        path = sandbox.create_cli_wrapper(repo)
        try:
            assert os.access(path, os.X_OK)
            with open(path) as f:
                content = f.read()
            assert content.startswith("#!/bin/sh\n")
            assert "docker run --rm" in content
            # Security: no secrets leak, repo mounted, caps dropped
            assert "-e GITHUB_TOKEN" in content
            assert "GITLAB_TOKEN" not in content
            assert "--cap-drop=ALL" in content
            assert "--pull=never" in content
            assert _TEST_SANDBOX_IMAGE in content
        finally:
            sandbox.cleanup()

    def test_create_cli_wrapper_script_is_executable(self, tmp_path: Path) -> None:
        """Should create an executable script file."""
        sandbox = ContainerSandbox(runtime="podman", image=_TEST_SANDBOX_IMAGE)
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        path = sandbox.create_cli_wrapper(repo)
        try:
            assert os.path.exists(path)
            assert os.stat(path).st_mode & stat.S_IXUSR  # executable
        finally:
            sandbox.cleanup()

    def test_cleanup_removes_script(self, tmp_path: Path) -> None:
        """Should remove the wrapper script on cleanup."""
        sandbox = ContainerSandbox(runtime="docker", image=_TEST_SANDBOX_IMAGE)
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        path = sandbox.create_cli_wrapper(repo)
        assert os.path.exists(path)
        sandbox.cleanup()
        assert not os.path.exists(path)

    def test_cleanup_idempotent(self, tmp_path: Path) -> None:
        """Should not raise when cleanup called multiple times."""
        sandbox = ContainerSandbox(runtime="docker", image=_TEST_SANDBOX_IMAGE)
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        sandbox.create_cli_wrapper(repo)
        sandbox.cleanup()
        sandbox.cleanup()  # Second call should not raise


class TestGetSandbox:
    """Tests for get_sandbox factory function."""

    def test_returns_bubblewrap_when_configured(self) -> None:
        """Should return BubblewrapSandbox when sandbox_method=bwrap."""
        settings = make_settings(sandbox_method="bwrap")
        sandbox = get_sandbox(settings)
        assert isinstance(sandbox, BubblewrapSandbox)

    def test_returns_docker_when_configured(self) -> None:
        """Should return ContainerSandbox(docker) when sandbox_method=docker."""
        settings = make_settings(sandbox_method="docker")
        sandbox = get_sandbox(settings)
        assert isinstance(sandbox, ContainerSandbox)
        assert sandbox._runtime == "docker"

    def test_returns_podman_when_configured(self) -> None:
        """Should return ContainerSandbox(podman) when sandbox_method=podman."""
        settings = make_settings(sandbox_method="podman")
        sandbox = get_sandbox(settings)
        assert isinstance(sandbox, ContainerSandbox)
        assert sandbox._runtime == "podman"

    def test_returns_noop_when_configured(self) -> None:
        """Should return NoopSandbox when sandbox_method=noop."""
        settings = make_settings(sandbox_method="noop")
        sandbox = get_sandbox(settings)
        assert isinstance(sandbox, NoopSandbox)
