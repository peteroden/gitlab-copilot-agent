"""Tests for the review engine prompt construction and run_review."""

from unittest.mock import AsyncMock, patch

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


def test_build_review_prompt_constructs_git_diff_command() -> None:
    prompt = build_review_prompt(_make_request())
    assert "git diff main...feature/x" in prompt
    assert "Add feature X" in prompt
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


@patch("gitlab_copilot_agent.review_engine.run_copilot_session")
async def test_run_review_delegates_to_copilot_session(
    mock_run_session: AsyncMock,
) -> None:
    mock_run_session.return_value = "Review result"

    settings = make_settings()
    req = _make_request()
    result = await run_review(settings, "/tmp/repo", req)

    assert result == "Review result"
    call_args = mock_run_session.call_args[1]
    assert call_args["system_prompt"] == SYSTEM_PROMPT
    assert "Add feature X" in call_args["user_prompt"]
    assert "git diff main...feature/x" in call_args["user_prompt"]
