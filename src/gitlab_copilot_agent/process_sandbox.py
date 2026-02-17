"""Copilot CLI binary resolution.

k8s pod boundary replaces all process-level sandbox methods (see ADR-0003).
"""

from __future__ import annotations

from pathlib import Path

import copilot as _copilot_pkg


def _get_real_cli_path() -> str:
    """Resolve the bundled Copilot CLI binary path."""
    cli_path = Path(_copilot_pkg.__file__).parent / "bin" / "copilot"
    if not cli_path.exists():
        msg = f"Bundled Copilot CLI not found at {cli_path}"
        raise RuntimeError(msg)
    return str(cli_path)
