"""Process-level sandboxing for Copilot SDK subprocess isolation."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Protocol

import copilot as _copilot_pkg


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

    def create_cli_wrapper(self, repo_path: str) -> str:
        """Create a wrapper script that runs the CLI inside bwrap."""
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

    def create_cli_wrapper(self, repo_path: str) -> str:  # noqa: ARG002
        """Return the real CLI path without sandboxing."""
        return _get_real_cli_path()

    def cleanup(self) -> None:
        """Nothing to clean up."""


def get_sandbox() -> ProcessSandbox:
    """Get the appropriate sandbox implementation.

    Returns BubblewrapSandbox if bwrap is available, NoopSandbox otherwise.
    """
    if shutil.which("bwrap"):
        return BubblewrapSandbox()
    return NoopSandbox()
