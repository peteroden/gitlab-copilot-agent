"""Tests for the shared Copilot session runner."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gitlab_copilot_agent.copilot_session import build_sdk_env, run_copilot_session
from gitlab_copilot_agent.repo_config import AgentConfig, RepoConfig
from tests.conftest import make_settings

PLUGIN_A = "copilot-plugin-a"
PLUGIN_B = "copilot-plugin-b"


def _make_event(event_type: str, content: str = "") -> SimpleNamespace:
    """Create a mock SDK event."""
    return SimpleNamespace(
        type=SimpleNamespace(value=event_type),
        data=SimpleNamespace(content=content),
    )


def _setup_mock_session(
    mock_client_class: MagicMock,
    events: list[SimpleNamespace],
) -> AsyncMock:
    """Wire up mock client/session that emits the given events on send."""
    mock_client = AsyncMock()
    mock_client_class.return_value = mock_client
    mock_session = AsyncMock()
    mock_session.on = MagicMock()
    mock_client.create_session.return_value = mock_session

    captured: dict[str, Callable[..., Any] | None] = {"handler": None}

    def capture_on(handler: Callable[..., Any]) -> None:
        captured["handler"] = handler

    mock_session.on.side_effect = capture_on

    async def fake_send(prompt: str, **_kwargs: object) -> None:
        assert captured["handler"] is not None
        for event in events:
            captured["handler"](event)

    mock_session.send.side_effect = fake_send
    return mock_client


@pytest.fixture
def _run(tmp_path: Path) -> Callable[..., Any]:
    """Return a helper that calls run_copilot_session with defaults."""

    async def _inner(**kwargs: Any) -> str:
        defaults = {
            "settings": make_settings(),
            "repo_path": str(tmp_path),
            "system_prompt": "System",
            "user_prompt": "User",
        }
        return await run_copilot_session(**cast(Any, defaults | kwargs))

    return _inner


@patch("gitlab_copilot_agent.copilot_session.discover_repo_config")
@patch("gitlab_copilot_agent.copilot_session.CopilotClient")
async def test_returns_last_message(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    _run: Any,
) -> None:
    mock_discover.return_value = RepoConfig()
    mock_client = _setup_mock_session(
        mock_client_class,
        [
            _make_event("assistant.message", "First"),
            _make_event("assistant.message", "Last"),
            _make_event("session.idle"),
        ],
    )

    assert await _run() == "Last"
    mock_client.start.assert_awaited_once()
    mock_client.stop.assert_awaited_once()


@patch("gitlab_copilot_agent.copilot_session.discover_repo_config")
@patch("gitlab_copilot_agent.copilot_session.CopilotClient")
async def test_empty_messages_returns_empty_string(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    _run: Any,
) -> None:
    mock_discover.return_value = RepoConfig()
    _setup_mock_session(mock_client_class, [_make_event("session.idle")])

    assert await _run() == ""


@patch("gitlab_copilot_agent.copilot_session.discover_repo_config")
@patch("gitlab_copilot_agent.copilot_session.CopilotClient")
async def test_passes_repo_config_to_session(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    _run: Any,
) -> None:
    mock_discover.return_value = RepoConfig(
        skill_directories=["/tmp/skills"],
        custom_agents=[AgentConfig(name="coder", prompt="Write code.")],
        instructions="Use strict typing.",
    )
    mock_client = _setup_mock_session(
        mock_client_class,
        [
            _make_event("assistant.message", "done"),
            _make_event("session.idle"),
        ],
    )

    await _run()

    session_kwargs = mock_client.create_session.call_args.kwargs
    assert session_kwargs["skill_directories"] == ["/tmp/skills"]
    assert len(session_kwargs["custom_agents"]) == 1
    assert "Use strict typing." in session_kwargs["system_message"]["content"]


def test_build_sdk_env_includes_only_allowed_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("GITLAB_TOKEN", "secret-gl-token")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "secret-webhook")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret-jira-token")

    env = build_sdk_env(github_token="gh-token")

    assert env["GITHUB_TOKEN"] == "gh-token"
    assert env["PATH"] == "/usr/bin"
    assert "GITLAB_TOKEN" not in env
    assert "GITLAB_WEBHOOK_SECRET" not in env
    assert "JIRA_API_TOKEN" not in env


def test_merge_plugins_deduplicates() -> None:
    from gitlab_copilot_agent.copilot_session import _merge_plugins

    result = _merge_plugins(["a", "b"], ["b", "c"])
    assert result == ["a", "b", "c"]


def test_merge_plugins_empty_inputs() -> None:
    from gitlab_copilot_agent.copilot_session import _merge_plugins

    assert _merge_plugins([], None) == []
    assert _merge_plugins([], []) == []


def test_merge_plugins_service_only() -> None:
    from gitlab_copilot_agent.copilot_session import _merge_plugins

    assert _merge_plugins(["a", "b"], None) == ["a", "b"]


def test_merge_plugins_repo_only() -> None:
    from gitlab_copilot_agent.copilot_session import _merge_plugins

    assert _merge_plugins([], ["x", "y"]) == ["x", "y"]


@patch("gitlab_copilot_agent.copilot_session.discover_repo_config")
@patch("gitlab_copilot_agent.copilot_session.CopilotClient")
async def test_session_creates_isolated_home(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    _run: Any,
) -> None:
    """Each session gets a fresh temp HOME that is cleaned up."""
    mock_discover.return_value = RepoConfig()
    _setup_mock_session(
        mock_client_class,
        [_make_event("assistant.message", "done"), _make_event("session.idle")],
    )

    await _run()

    # Verify the SDK env got a custom HOME (not the process HOME)
    subprocess_config = mock_client_class.call_args[0][0]
    sdk_home = subprocess_config.env["HOME"]
    assert "copilot-session-" in sdk_home
    # Temp dir should be cleaned up after session
    assert not Path(sdk_home).exists()


@patch("gitlab_copilot_agent.plugin_manager.setup_plugins", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.copilot_session.discover_repo_config")
@patch("gitlab_copilot_agent.copilot_session.CopilotClient")
async def test_session_installs_plugins(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    mock_setup: AsyncMock,
    tmp_path: Path,
) -> None:
    """Plugins are installed into the session HOME."""
    mock_discover.return_value = RepoConfig()
    _setup_mock_session(
        mock_client_class,
        [_make_event("assistant.message", "done"), _make_event("session.idle")],
    )

    settings = make_settings(
        copilot_plugins=["svc-plugin"],
        copilot_plugin_marketplaces=["https://mp.example.com"],
    )
    await run_copilot_session(
        settings=settings,
        repo_path=str(tmp_path),
        system_prompt="System",
        user_prompt="User",
        plugins=["repo-plugin"],
    )

    mock_setup.assert_awaited_once()
    call_args = mock_setup.call_args
    home_dir = call_args[0][0]
    plugins = call_args[0][1]
    marketplaces = call_args[0][2]
    assert "copilot-session-" in home_dir
    assert "svc-plugin" in plugins
    assert "repo-plugin" in plugins
    assert marketplaces == ["https://mp.example.com"]


@patch("gitlab_copilot_agent.copilot_session.discover_repo_config")
@patch("gitlab_copilot_agent.copilot_session.CopilotClient")
async def test_session_no_plugins_skips_setup(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    _run: Any,
) -> None:
    """No plugins configured means setup_plugins is not called."""
    mock_discover.return_value = RepoConfig()
    _setup_mock_session(
        mock_client_class,
        [_make_event("assistant.message", "done"), _make_event("session.idle")],
    )

    with patch(
        "gitlab_copilot_agent.plugin_manager.setup_plugins", new_callable=AsyncMock
    ) as mock_setup:
        await _run()
        mock_setup.assert_not_awaited()


@patch("gitlab_copilot_agent.copilot_session.discover_repo_config")
@patch("gitlab_copilot_agent.copilot_session.CopilotClient")
async def test_auth_failure_raises_immediately(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    _run: Any,
) -> None:
    """Unauthenticated client raises RuntimeError before session creation."""
    mock_discover.return_value = RepoConfig()
    mock_client = AsyncMock()
    mock_client_class.return_value = mock_client
    mock_client.get_auth_status.return_value = SimpleNamespace(
        authType=None,
        isAuthenticated=False,
    )

    with pytest.raises(RuntimeError, match="Copilot authentication failed"):
        await _run()

    # Session should never be created
    mock_client.create_session.assert_not_awaited()


@patch("gitlab_copilot_agent.copilot_session.discover_repo_config")
@patch("gitlab_copilot_agent.copilot_session.CopilotClient")
async def test_session_error_raises_runtime_error(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    _run: Any,
) -> None:
    """session.error event raises RuntimeError with error details."""
    mock_discover.return_value = RepoConfig()
    error_event = SimpleNamespace(
        type=SimpleNamespace(value="session.error"),
        data=SimpleNamespace(
            error_type="authentication",
            message="Session was not created with authentication info",
            content="",
        ),
    )
    _setup_mock_session(
        mock_client_class,
        [error_event],
    )

    with pytest.raises(RuntimeError, match="authentication"):
        await _run()


@patch("gitlab_copilot_agent.copilot_session.discover_repo_config")
@patch("gitlab_copilot_agent.copilot_session.CopilotClient")
async def test_session_error_on_retry_raises(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    _run: Any,
) -> None:
    """session.error during retry phase also raises RuntimeError."""
    mock_discover.return_value = RepoConfig()

    call_count = {"n": 0}

    mock_client = AsyncMock()
    mock_client_class.return_value = mock_client
    mock_session = AsyncMock()
    mock_session.on = MagicMock()
    mock_client.create_session.return_value = mock_session

    captured: dict[str, Callable[..., Any] | None] = {"handler": None}

    def capture_on(handler: Callable[..., Any]) -> None:
        captured["handler"] = handler

    mock_session.on.side_effect = capture_on

    async def fake_send(prompt: str, **_kwargs: object) -> None:
        assert captured["handler"] is not None
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call succeeds with a message that fails validation
            captured["handler"](_make_event("assistant.message", "no json here"))
            captured["handler"](_make_event("session.idle"))
        else:
            # Retry gets a session error
            error_event = SimpleNamespace(
                type=SimpleNamespace(value="session.error"),
                data=SimpleNamespace(
                    error_type="quota",
                    message="Rate limit exceeded",
                    content="",
                ),
            )
            captured["handler"](error_event)

    mock_session.send.side_effect = fake_send

    def validator(result: str) -> str | None:
        return "Try again" if "files_changed" not in result else None

    with pytest.raises(RuntimeError, match="quota"):
        await _run(validate_response=validator)
