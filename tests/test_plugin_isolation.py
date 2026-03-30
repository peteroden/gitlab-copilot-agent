"""Integration tests for plugin session isolation.

Verifies that per-session HOME isolation prevents plugin state from
leaking between repos/sessions. Uses mocked subprocess calls — does
not require a real Copilot CLI or public marketplaces.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gitlab_copilot_agent.copilot_session import _merge_plugins, run_copilot_session
from gitlab_copilot_agent.repo_config import RepoConfig
from tests.conftest import make_settings

# Deterministic fixture plugin specs (no external dependencies)
SVC_PLUGIN = "svc-analytics"
REPO_A_PLUGIN = "repo-a-linter"
REPO_B_PLUGIN = "repo-b-formatter"
MARKETPLACE = "https://marketplace.example.com"

_SESSION_MOD = "gitlab_copilot_agent.copilot_session"
_PLUGIN_MOD = "gitlab_copilot_agent.plugin_manager"


def _make_event(event_type: str, content: str = "") -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        type=SimpleNamespace(value=event_type),
        data=SimpleNamespace(content=content),
    )


def _setup_mock_client(mock_client_class: MagicMock) -> None:
    """Wire up a mock CopilotClient that emits idle on send."""
    from collections.abc import Callable
    from typing import Any

    mock_client = AsyncMock()
    mock_client_class.return_value = mock_client
    mock_session = AsyncMock()
    mock_session.on = MagicMock()
    mock_client.create_session.return_value = mock_session

    captured: dict[str, Callable[..., Any] | None] = {"handler": None}

    def capture_on(handler: Callable[..., Any]) -> None:
        captured["handler"] = handler

    mock_session.on.side_effect = capture_on

    async def fake_send(msg: object) -> None:
        assert captured["handler"] is not None
        captured["handler"](_make_event("assistant.message", "done"))
        captured["handler"](_make_event("session.idle"))

    mock_session.send.side_effect = fake_send


class TestPluginMergeAndDedupe:
    """Merge and dedupe behavior for service + repo plugins."""

    def test_service_and_repo_merged(self) -> None:
        result = _merge_plugins([SVC_PLUGIN], [REPO_A_PLUGIN])
        assert result == [SVC_PLUGIN, REPO_A_PLUGIN]

    def test_duplicates_removed(self) -> None:
        result = _merge_plugins([SVC_PLUGIN, REPO_A_PLUGIN], [REPO_A_PLUGIN, REPO_B_PLUGIN])
        assert result == [SVC_PLUGIN, REPO_A_PLUGIN, REPO_B_PLUGIN]

    def test_service_order_preserved(self) -> None:
        result = _merge_plugins(["c", "a", "b"], ["d", "a"])
        assert result == ["c", "a", "b", "d"]

    def test_repo_none_uses_service_only(self) -> None:
        assert _merge_plugins([SVC_PLUGIN], None) == [SVC_PLUGIN]

    def test_both_empty(self) -> None:
        assert _merge_plugins([], []) == []


class TestSessionHomeIsolation:
    """Each session gets its own HOME; no state leaks between sessions."""

    @patch(f"{_PLUGIN_MOD}.setup_plugins", new_callable=AsyncMock)
    @patch(f"{_SESSION_MOD}.discover_repo_config")
    @patch(f"{_SESSION_MOD}.CopilotClient")
    async def test_two_sessions_get_different_homes(
        self,
        mock_client_class: MagicMock,
        mock_discover: MagicMock,
        mock_setup: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Repo A and repo B sessions must use distinct HOME directories."""
        mock_discover.return_value = RepoConfig()
        homes: list[str] = []

        async def capture_home(home_dir: str, *_args: object, **_kw: object) -> None:
            homes.append(home_dir)

        mock_setup.side_effect = capture_home

        settings = make_settings(copilot_plugins=[SVC_PLUGIN])

        for repo_plugins in [[REPO_A_PLUGIN], [REPO_B_PLUGIN]]:
            _setup_mock_client(mock_client_class)
            await run_copilot_session(
                settings=settings,
                repo_path=str(tmp_path),
                system_prompt="test",
                user_prompt="test",
                plugins=repo_plugins,
            )

        assert len(homes) == 2
        assert homes[0] != homes[1]
        # Both should be cleaned up
        assert not Path(homes[0]).exists()
        assert not Path(homes[1]).exists()

    @patch(f"{_PLUGIN_MOD}.setup_plugins", new_callable=AsyncMock)
    @patch(f"{_SESSION_MOD}.discover_repo_config")
    @patch(f"{_SESSION_MOD}.CopilotClient")
    async def test_repo_a_plugins_not_visible_to_repo_b(
        self,
        mock_client_class: MagicMock,
        mock_discover: MagicMock,
        mock_setup: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Plugin install calls for repo A must not include repo B specs."""
        mock_discover.return_value = RepoConfig()
        install_calls: list[tuple[str, list[str]]] = []

        async def capture_install(
            home_dir: str, plugins: list[str], *_args: object, **_kw: object
        ) -> None:
            install_calls.append((home_dir, list(plugins)))

        mock_setup.side_effect = capture_install

        settings = make_settings(copilot_plugins=[SVC_PLUGIN])

        # Session for repo A
        _setup_mock_client(mock_client_class)
        await run_copilot_session(
            settings=settings,
            repo_path=str(tmp_path),
            system_prompt="test",
            user_prompt="test",
            plugins=[REPO_A_PLUGIN],
        )

        # Session for repo B
        _setup_mock_client(mock_client_class)
        await run_copilot_session(
            settings=settings,
            repo_path=str(tmp_path),
            system_prompt="test",
            user_prompt="test",
            plugins=[REPO_B_PLUGIN],
        )

        assert len(install_calls) == 2
        repo_a_plugins = install_calls[0][1]
        repo_b_plugins = install_calls[1][1]
        # Repo A sees service + repo A only
        assert SVC_PLUGIN in repo_a_plugins
        assert REPO_A_PLUGIN in repo_a_plugins
        assert REPO_B_PLUGIN not in repo_a_plugins
        # Repo B sees service + repo B only
        assert SVC_PLUGIN in repo_b_plugins
        assert REPO_B_PLUGIN in repo_b_plugins
        assert REPO_A_PLUGIN not in repo_b_plugins

    @patch(f"{_PLUGIN_MOD}.setup_plugins", new_callable=AsyncMock)
    @patch(f"{_SESSION_MOD}.discover_repo_config")
    @patch(f"{_SESSION_MOD}.CopilotClient")
    async def test_service_plugins_apply_to_all_sessions(
        self,
        mock_client_class: MagicMock,
        mock_discover: MagicMock,
        mock_setup: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Service-level plugins appear in every session."""
        mock_discover.return_value = RepoConfig()
        install_calls: list[list[str]] = []

        async def capture_install(
            home_dir: str, plugins: list[str], *_args: object, **_kw: object
        ) -> None:
            install_calls.append(list(plugins))

        mock_setup.side_effect = capture_install

        settings = make_settings(copilot_plugins=[SVC_PLUGIN])

        for repo_plugins in [[REPO_A_PLUGIN], [REPO_B_PLUGIN], []]:
            _setup_mock_client(mock_client_class)
            await run_copilot_session(
                settings=settings,
                repo_path=str(tmp_path),
                system_prompt="test",
                user_prompt="test",
                plugins=repo_plugins or None,
            )

        assert len(install_calls) == 3
        for call_plugins in install_calls:
            assert SVC_PLUGIN in call_plugins

    @patch(f"{_SESSION_MOD}.discover_repo_config")
    @patch(f"{_SESSION_MOD}.CopilotClient")
    async def test_home_cleaned_up_on_session_error(
        self,
        mock_client_class: MagicMock,
        mock_discover: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Temp HOME is removed even when the session raises."""
        mock_discover.return_value = RepoConfig()
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.start.side_effect = RuntimeError("startup failed")

        with pytest.raises(RuntimeError, match="startup failed"):
            await run_copilot_session(
                settings=make_settings(),
                repo_path=str(tmp_path),
                system_prompt="test",
                user_prompt="test",
            )

        # All copilot-session- dirs should be cleaned up
        import tempfile

        remaining = list(Path(tempfile.gettempdir()).glob("copilot-session-*"))
        assert len(remaining) == 0
