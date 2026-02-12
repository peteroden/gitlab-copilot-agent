"""Tests for the review engine prompt construction and run_review."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gitlab_copilot_agent.review_engine import (
    SYSTEM_PROMPT,
    ReviewRequest,
    build_review_prompt,
    run_review,
)
from tests.conftest import make_settings


def _make_request() -> ReviewRequest:
    return ReviewRequest(
        title="Add feature X",
        description="Implements feature X",
        source_branch="feature/x",
        target_branch="main",
    )


def test_system_prompt_contains_review_guidance() -> None:
    assert "security" in SYSTEM_PROMPT.lower()
    assert "json" in SYSTEM_PROMPT.lower()
    assert "severity" in SYSTEM_PROMPT.lower()


def test_build_review_prompt_includes_title() -> None:
    prompt = build_review_prompt(_make_request())
    assert "Add feature X" in prompt


def test_build_review_prompt_includes_git_diff_command() -> None:
    prompt = build_review_prompt(_make_request())
    assert "git diff main...feature/x" in prompt


def test_build_review_prompt_includes_description() -> None:
    prompt = build_review_prompt(_make_request())
    assert "Implements feature X" in prompt


def test_build_review_prompt_handles_no_description() -> None:
    req = ReviewRequest(
        title="No desc",
        description=None,
        source_branch="feat",
        target_branch="main",
    )
    prompt = build_review_prompt(req)
    assert "(none)" in prompt


def _make_event(event_type: str, content: str = "") -> SimpleNamespace:
    """Create a mock SDK event."""
    return SimpleNamespace(
        type=SimpleNamespace(value=event_type),
        data=SimpleNamespace(content=content),
    )


@patch("gitlab_copilot_agent.review_engine.discover_repo_config")
@patch("gitlab_copilot_agent.review_engine.CopilotClient")
async def test_run_review_returns_last_message(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    tmp_path: Path,
) -> None:
    from gitlab_copilot_agent.repo_config import RepoConfig

    mock_discover.return_value = RepoConfig()

    mock_client = AsyncMock()
    mock_client_class.return_value = mock_client

    mock_session = AsyncMock()
    mock_session.on = MagicMock()
    mock_client.create_session.return_value = mock_session

    captured_handler = None

    def capture_on(handler: object) -> None:
        nonlocal captured_handler
        captured_handler = handler

    mock_session.on.side_effect = capture_on

    async def fake_send(msg: object) -> None:
        assert captured_handler is not None
        captured_handler(_make_event("assistant.message", "I'll review..."))
        captured_handler(_make_event("assistant.message", "[{\"file\": \"a.py\"}]"))
        captured_handler(_make_event("session.idle"))

    mock_session.send.side_effect = fake_send

    settings = make_settings()
    result = await run_review(settings, str(tmp_path), _make_request())

    assert "[{\"file\": \"a.py\"}]" in result
    mock_client.start.assert_awaited_once()
    mock_client.stop.assert_awaited_once()
    mock_session.destroy.assert_awaited_once()


@patch("gitlab_copilot_agent.review_engine.discover_repo_config")
@patch("gitlab_copilot_agent.review_engine.CopilotClient")
async def test_run_review_empty_messages(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    tmp_path: Path,
) -> None:
    from gitlab_copilot_agent.repo_config import RepoConfig

    mock_discover.return_value = RepoConfig()

    mock_client = AsyncMock()
    mock_client_class.return_value = mock_client
    mock_session = AsyncMock()
    mock_session.on = MagicMock()
    mock_client.create_session.return_value = mock_session

    captured_handler = None

    def capture_on(handler: object) -> None:
        nonlocal captured_handler
        captured_handler = handler

    mock_session.on.side_effect = capture_on

    async def fake_send(msg: object) -> None:
        assert captured_handler is not None
        captured_handler(_make_event("session.idle"))

    mock_session.send.side_effect = fake_send

    settings = make_settings()
    result = await run_review(settings, str(tmp_path), _make_request())
    assert result == ""


@patch("gitlab_copilot_agent.review_engine.discover_repo_config")
@patch("gitlab_copilot_agent.review_engine.CopilotClient")
async def test_run_review_passes_repo_config(
    mock_client_class: MagicMock,
    mock_discover: MagicMock,
    tmp_path: Path,
) -> None:
    from gitlab_copilot_agent.repo_config import RepoConfig

    mock_discover.return_value = RepoConfig(
        skill_directories=["/tmp/skills"],
        custom_agents=[{"name": "reviewer", "prompt": "Review code."}],
        instructions="Use type hints.",
    )

    mock_client = AsyncMock()
    mock_client_class.return_value = mock_client
    mock_session = AsyncMock()
    mock_session.on = MagicMock()
    mock_client.create_session.return_value = mock_session

    captured_handler = None

    def capture_on(handler: object) -> None:
        nonlocal captured_handler
        captured_handler = handler

    mock_session.on.side_effect = capture_on

    async def fake_send(msg: object) -> None:
        assert captured_handler is not None
        captured_handler(_make_event("assistant.message", "review done"))
        captured_handler(_make_event("session.idle"))

    mock_session.send.side_effect = fake_send

    settings = make_settings()
    await run_review(settings, str(tmp_path), _make_request())

    session_opts = mock_client.create_session.call_args[0][0]
    assert session_opts["skill_directories"] == ["/tmp/skills"]
    assert len(session_opts["custom_agents"]) == 1
    assert "Use type hints." in session_opts["system_message"]["content"]
