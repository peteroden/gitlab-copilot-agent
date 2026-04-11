"""Repository tarball creation and extraction."""

from __future__ import annotations

import asyncio
import io
import tarfile
import tempfile
from pathlib import Path

CLONE_DIR_PREFIX = "mr-review-"
_GIT_CONFIG_SUFFIX = "/.git/config"


def _exclude_git_credentials(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Exclude .git/config from tarballs to prevent credential leakage.

    git_clone embeds oauth2:{token}@ in the origin URL which persists in
    .git/config.  The runner only needs local git operations (add, diff)
    so the remote config is unnecessary.
    """
    if info.name.endswith(_GIT_CONFIG_SUFFIX) or info.name == ".git/config":
        return None
    return info


async def tar_repo_to_bytes(repo_path: str) -> bytes:
    """Create a gzip-compressed tarball of a repository directory.

    Excludes ``.git/config`` to prevent credential leakage (the clone URL
    may contain embedded tokens).
    """

    def _tar() -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(repo_path, arcname=".", filter=_exclude_git_credentials)
        return buf.getvalue()

    return await asyncio.to_thread(_tar)


async def extract_repo_tarball(data: bytes, clone_dir: str | None = None) -> Path:
    """Extract a repo tarball to a temp directory.

    Uses ``filter='data'`` to strip device nodes, setuid bits, etc.
    """

    def _extract() -> Path:
        base = clone_dir or tempfile.gettempdir()
        target = Path(tempfile.mkdtemp(prefix=CLONE_DIR_PREFIX, dir=base))
        buf = io.BytesIO(data)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            tar.extractall(path=str(target), filter="data")
        return target

    return await asyncio.to_thread(_extract)
