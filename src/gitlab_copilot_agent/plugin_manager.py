"""Copilot CLI plugin manager — runtime plugin installation into isolated HOME."""

import asyncio
import os
from urllib.parse import urlparse

import structlog

from gitlab_copilot_agent.process_sandbox import get_real_cli_path

log = structlog.get_logger()

_INSTALL_TIMEOUT = 120  # seconds per plugin install


def _sanitize_url(url: str) -> str:
    """Strip credentials and query params from a URL for safe logging."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}{parsed.path}" if parsed.hostname else url


async def _run_cli(args: list[str], *, home_dir: str, timeout: float = _INSTALL_TIMEOUT) -> bytes:
    """Run a copilot CLI command, killing the subprocess on timeout."""
    cli = get_real_cli_path()
    env = {"HOME": home_dir, "PATH": os.environ.get("PATH", "")}
    proc = await asyncio.create_subprocess_exec(
        cli,
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"Plugin command timed out after {timeout}s: copilot {' '.join(args)}"
        ) from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"Plugin command failed (rc={proc.returncode}): copilot {' '.join(args)}: "
            f"{stderr.decode().strip()}"
        )
    return stderr


async def add_marketplace(home_dir: str, marketplace_url: str) -> None:
    """Register a custom plugin marketplace in the given HOME."""
    await _run_cli(["plugin", "marketplace", "add", marketplace_url], home_dir=home_dir)
    await log.ainfo("marketplace_added", url=_sanitize_url(marketplace_url))


async def install_plugin(home_dir: str, plugin_spec: str) -> None:
    """Install a single Copilot CLI plugin into the given HOME."""
    await _run_cli(["plugin", "install", plugin_spec], home_dir=home_dir)
    await log.ainfo("plugin_installed", plugin=plugin_spec)


async def setup_plugins(
    home_dir: str,
    plugins: list[str],
    marketplaces: list[str] | None = None,
) -> None:
    """Install marketplaces and plugins into an isolated HOME directory.

    Called once per Copilot session with a fresh temp HOME.
    """
    if not plugins and not marketplaces:
        return

    for url in marketplaces or []:
        await add_marketplace(home_dir, url)

    seen: set[str] = set()
    for spec in plugins:
        if spec in seen:
            continue
        seen.add(spec)
        await install_plugin(home_dir, spec)

    await log.ainfo(
        "plugin_setup_complete",
        plugin_count=len(seen),
        marketplace_count=len(marketplaces or []),
    )
