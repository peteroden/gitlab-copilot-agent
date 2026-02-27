"""Tests for the review engine prompt construction and run_review."""

from unittest.mock import AsyncMock

from gitlab_copilot_agent.prompt_defaults import get_prompt
from gitlab_copilot_agent.review_engine import (
    ReviewRequest,
    build_review_prompt,
    run_review,
)
from tests.conftest import EXAMPLE_CLONE_URL, make_settings


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


async def test_run_review_delegates_to_executor() -> None:
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "Review result"

    settings = make_settings()
    req = _make_request()
    result = await run_review(mock_executor, settings, "/tmp/repo", EXAMPLE_CLONE_URL, req)

    assert result == "Review result"
    task = mock_executor.execute.call_args[0][0]
    assert task.system_prompt == get_prompt(settings, "review")
    assert "Add feature X" in task.user_prompt
    assert "git diff main...feature/x" in task.user_prompt
