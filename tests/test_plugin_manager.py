"""Tests for Copilot CLI plugin manager — installation and isolation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gitlab_copilot_agent.plugin_manager import (
    _sanitize_url,
    add_marketplace,
    install_plugin,
    setup_plugins,
)

PLUGIN_A = "copilot-plugin-a"
PLUGIN_B = "copilot-plugin-b"
MARKETPLACE_URL = "https://marketplace.example.com"
FAKE_HOME = "/home/copilot-session-test"
FAKE_CLI = "/usr/local/bin/copilot"

_MODULE = "gitlab_copilot_agent.plugin_manager"


def _mock_process(returncode: int = 0, stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestAddMarketplace:
    @patch(f"{_MODULE}.get_real_cli_path", return_value=FAKE_CLI)
    @patch("asyncio.create_subprocess_exec")
    async def test_success(self, mock_exec: AsyncMock, _cli: MagicMock) -> None:
        mock_exec.return_value = _mock_process()
        await add_marketplace(FAKE_HOME, MARKETPLACE_URL)
        mock_exec.assert_awaited_once()
        args = mock_exec.call_args[0]
        assert args == (FAKE_CLI, "plugin", "marketplace", "add", MARKETPLACE_URL)
        env = mock_exec.call_args[1]["env"]
        assert env["HOME"] == FAKE_HOME

    @patch(f"{_MODULE}.get_real_cli_path", return_value=FAKE_CLI)
    @patch("asyncio.create_subprocess_exec")
    async def test_failure_raises(self, mock_exec: AsyncMock, _cli: MagicMock) -> None:
        mock_exec.return_value = _mock_process(returncode=1, stderr=b"not found")
        with pytest.raises(RuntimeError, match="Plugin command failed"):
            await add_marketplace(FAKE_HOME, MARKETPLACE_URL)


class TestInstallPlugin:
    @patch(f"{_MODULE}.get_real_cli_path", return_value=FAKE_CLI)
    @patch("asyncio.create_subprocess_exec")
    async def test_success(self, mock_exec: AsyncMock, _cli: MagicMock) -> None:
        mock_exec.return_value = _mock_process()
        await install_plugin(FAKE_HOME, PLUGIN_A)
        args = mock_exec.call_args[0]
        assert args == (FAKE_CLI, "plugin", "install", PLUGIN_A)

    @patch(f"{_MODULE}.get_real_cli_path", return_value=FAKE_CLI)
    @patch("asyncio.create_subprocess_exec")
    async def test_failure_raises(self, mock_exec: AsyncMock, _cli: MagicMock) -> None:
        mock_exec.return_value = _mock_process(returncode=1, stderr=b"install failed")
        with pytest.raises(RuntimeError, match="Plugin command failed"):
            await install_plugin(FAKE_HOME, PLUGIN_A)

    @patch(f"{_MODULE}.get_real_cli_path", return_value=FAKE_CLI)
    @patch("asyncio.create_subprocess_exec")
    async def test_timeout_kills_process(self, mock_exec: AsyncMock, _cli: MagicMock) -> None:
        proc = _mock_process()
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        mock_exec.return_value = proc
        with pytest.raises(RuntimeError, match="timed out"):
            await install_plugin(FAKE_HOME, PLUGIN_A)
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()


class TestSetupPlugins:
    @patch(f"{_MODULE}.install_plugin", new_callable=AsyncMock)
    @patch(f"{_MODULE}.add_marketplace", new_callable=AsyncMock)
    async def test_no_plugins_no_calls(self, mock_mp: AsyncMock, mock_inst: AsyncMock) -> None:
        await setup_plugins(FAKE_HOME, [], None)
        mock_mp.assert_not_awaited()
        mock_inst.assert_not_awaited()

    @patch(f"{_MODULE}.install_plugin", new_callable=AsyncMock)
    @patch(f"{_MODULE}.add_marketplace", new_callable=AsyncMock)
    async def test_installs_plugins_and_marketplaces(
        self, mock_mp: AsyncMock, mock_inst: AsyncMock
    ) -> None:
        await setup_plugins(FAKE_HOME, [PLUGIN_A, PLUGIN_B], [MARKETPLACE_URL])
        mock_mp.assert_awaited_once_with(FAKE_HOME, MARKETPLACE_URL)
        assert mock_inst.await_count == 2
        mock_inst.assert_any_await(FAKE_HOME, PLUGIN_A)
        mock_inst.assert_any_await(FAKE_HOME, PLUGIN_B)

    @patch(f"{_MODULE}.install_plugin", new_callable=AsyncMock)
    @patch(f"{_MODULE}.add_marketplace", new_callable=AsyncMock)
    async def test_deduplicates_plugins(self, mock_mp: AsyncMock, mock_inst: AsyncMock) -> None:
        await setup_plugins(FAKE_HOME, [PLUGIN_A, PLUGIN_A, PLUGIN_B], None)
        assert mock_inst.await_count == 2

    @patch(f"{_MODULE}.install_plugin", new_callable=AsyncMock)
    @patch(f"{_MODULE}.add_marketplace", new_callable=AsyncMock)
    async def test_plugins_only(self, mock_mp: AsyncMock, mock_inst: AsyncMock) -> None:
        await setup_plugins(FAKE_HOME, [PLUGIN_A], None)
        mock_mp.assert_not_awaited()
        mock_inst.assert_awaited_once_with(FAKE_HOME, PLUGIN_A)


class TestSanitizeUrl:
    def test_strips_credentials(self) -> None:
        assert _sanitize_url("https://user:pass@host.com/path") == "https://host.com/path"

    def test_strips_query_params(self) -> None:
        assert _sanitize_url("https://host.com/path?token=secret") == "https://host.com/path"

    def test_plain_url_unchanged(self) -> None:
        assert _sanitize_url(MARKETPLACE_URL) == MARKETPLACE_URL
