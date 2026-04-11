"""Patch validation and application."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.telemetry import get_tracer

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger()
_tracer = get_tracer(__name__)

_GIT_TIMEOUT = 60
MAX_PATCH_SIZE = 10 * 1024 * 1024  # 10 MB

_PATH_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
# Match only actual file-header lines (not hunk content that happens to start with ---/+++)
_FILE_HEADER_RE = re.compile(r"^(diff --git |--- [ab]/|\+\+\+ [ab]/)")


def _validate_patch(patch: str) -> None:
    """Reject patches containing path traversal sequences in file headers."""
    for line in patch.splitlines():
        if _FILE_HEADER_RE.match(line) and _PATH_TRAVERSAL_RE.search(line):
            raise ValueError(f"Patch contains path traversal: {line!r}")


async def git_apply_patch(repo_path: Path, patch: str) -> None:
    """Apply a unified diff to *repo_path* using ``git apply --3way``.

    The patch is piped via stdin — no temp files on disk.
    Raises ValueError if the patch contains path traversal sequences.
    Raises RuntimeError if git apply fails.
    """
    _validate_patch(patch)
    with _tracer.start_as_current_span("git.apply_patch"):
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_path),
            "apply",
            "--whitespace=nowarn",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=patch.encode()), timeout=_GIT_TIMEOUT
            )
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise RuntimeError("git apply timed out") from e
        if proc.returncode != 0:
            raise RuntimeError(f"git apply failed: {stderr.decode().strip()}")
        await log.ainfo("patch_applied", repo=str(repo_path))
