"""Process-level sandboxing for Copilot SDK subprocess isolation."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, Protocol

import copilot as _copilot_pkg
import structlog

from gitlab_copilot_agent.config import Settings

log = structlog.get_logger()


class ProcessSandbox(Protocol):
    """Protocol for process-level sandboxing of the Copilot CLI subprocess."""

    def create_cli_wrapper(self, repo_path: str) -> str:
        """Create a sandboxed CLI wrapper and return its path.

        The returned path is passed as cli_path to CopilotClientOptions.
        Caller must call cleanup() when done.
        """
        ...

    def cleanup(self) -> None:
        """Clean up any resources created by the sandbox."""
        ...

    def preflight(self) -> None:
        """Validate runtime dependencies. Raise RuntimeError if unavailable."""
        ...


def _get_real_cli_path() -> str:
    """Resolve the bundled Copilot CLI binary path."""
    cli_path = Path(_copilot_pkg.__file__).parent / "bin" / "copilot"
    if not cli_path.exists():
        msg = f"Bundled Copilot CLI not found at {cli_path}"
        raise RuntimeError(msg)
    return str(cli_path)


class BubblewrapSandbox:
    """Sandbox using bubblewrap (bwrap) for filesystem isolation.

    Makes system directories read-only, provides throwaway /tmp and /home,
    and only allows writes to the cloned repo directory.
    """

    def __init__(self) -> None:
        self._script_path: str | None = None

    def preflight(self) -> None:
        """Validate that bwrap is available and functional."""
        if not shutil.which("bwrap"):
            raise RuntimeError("bwrap binary not found on PATH")
        try:
            subprocess.run(
                ["bwrap", "--version"],
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise RuntimeError(f"bwrap preflight check failed: {e}") from e

    def create_cli_wrapper(self, repo_path: str) -> str:
        """Create a wrapper script that runs the CLI inside bwrap."""
        log.debug("sandbox_wrapper_created", method="bwrap", repo_path=repo_path)
        real_cli = _get_real_cli_path()
        safe_cli = shlex.quote(real_cli)
        safe_repo = shlex.quote(repo_path)

        # Mount the CLI binary's directory tree read-only so it's
        # accessible inside the bwrap namespace. Use the top-level
        # virtualenv or install root (3 levels up from bin/copilot).
        cli_root = str(Path(real_cli).parent.parent)
        safe_cli_root = shlex.quote(cli_root)

        # Build bwrap command that:
        # - Mounts system dirs read-only (prevents global installs)
        # - Mounts CLI package dir read-only (SDK binary access)
        # - Creates throwaway /tmp and /home (tmpfs)
        # - Mounts repo dir read-write
        # - Shares network (SDK needs GitHub API)
        # - Dies with parent process
        script_content = f"""#!/bin/sh
exec bwrap \\
  --ro-bind /usr /usr \\
  --ro-bind /bin /bin \\
  --ro-bind /lib /lib \\
  --ro-bind /sbin /sbin \\
  --symlink usr/lib /lib64 \\
  --ro-bind /etc/resolv.conf /etc/resolv.conf \\
  --ro-bind /etc/ssl /etc/ssl \\
  --ro-bind /etc/ca-certificates /etc/ca-certificates \\
  --tmpfs /tmp \\
  --tmpfs /home \\
  --tmpfs /var/tmp \\
  --ro-bind {safe_cli_root} {safe_cli_root} \\
  --bind {safe_repo} {safe_repo} \\
  --ro-bind /proc /proc \\
  --dev /dev \\
  --unshare-all \\
  --share-net \\
  --die-with-parent \\
  {safe_cli} "$@"
"""
        fd, script_path = tempfile.mkstemp(prefix="copilot-bwrap-", suffix=".sh")
        try:
            os.write(fd, script_content.encode())
        finally:
            os.close(fd)

        os.chmod(script_path, stat.S_IRWXU)
        self._script_path = script_path
        return script_path

    def cleanup(self) -> None:
        """Remove the wrapper script."""
        if self._script_path:
            with contextlib.suppress(OSError):
                os.unlink(self._script_path)
            self._script_path = None


class NoopSandbox:
    """No-op sandbox that passes through to the real CLI.

    Used when bwrap is not available (e.g., macOS, CI without capabilities).
    """

    def preflight(self) -> None:
        """No-op sandbox is always available."""

    def create_cli_wrapper(self, repo_path: str) -> str:  # noqa: ARG002
        """Return the real CLI path without sandboxing."""
        log.debug("sandbox_wrapper_created", method="noop", repo_path=repo_path)
        return _get_real_cli_path()

    def cleanup(self) -> None:
        """Nothing to clean up."""


class ContainerSandbox:
    """Sandbox using Docker or Podman containers (not yet implemented).

    Stub implementation for configuration dispatch — implementation deferred.
    """

    def __init__(self, runtime: Literal["docker", "podman"]) -> None:
        self._runtime = runtime

    def preflight(self) -> None:
        """Fail fast — container sandbox is not yet implemented."""
        raise NotImplementedError(
            f"{self._runtime} sandbox is not yet implemented. "
            f"Use SANDBOX_METHOD=bwrap or SANDBOX_METHOD=noop."
        )

    def create_cli_wrapper(self, repo_path: str) -> str:  # noqa: ARG002
        """Not yet implemented."""
        raise NotImplementedError("Container sandbox not yet implemented")

    def cleanup(self) -> None:
        """Nothing to clean up yet."""


def get_sandbox(settings: Settings) -> ProcessSandbox:
    """Get the configured sandbox implementation.

    Raises ValueError if sandbox_method is invalid (should be prevented by Pydantic).
    """
    match settings.sandbox_method:
        case "bwrap":
            return BubblewrapSandbox()
        case "docker":
            return ContainerSandbox(runtime="docker")
        case "podman":
            return ContainerSandbox(runtime="podman")
        case "noop":
            return NoopSandbox()
        case _:  # pragma: no cover
            raise ValueError(f"Invalid sandbox_method: {settings.sandbox_method}")
