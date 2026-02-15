"""Tests for the shared Copilot session runner."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gitlab_copilot_agent.copilot_session import build_sdk_env, run_copilot_session
from gitlab_copilot_agent.repo_config import RepoConfig
from tests.conftest import make_settings


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

    async def fake_send(msg: object) -> None:
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
        return await run_copilot_session(**(defaults | kwargs))

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
        custom_agents=[{"name": "coder", "prompt": "Write code."}],
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

    session_opts = mock_client.create_session.call_args[0][0]
    assert session_opts["skill_directories"] == ["/tmp/skills"]
    assert len(session_opts["custom_agents"]) == 1
    assert "Use strict typing." in session_opts["system_message"]["content"]


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
