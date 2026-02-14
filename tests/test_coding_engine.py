"""Tests for the coding engine."""

from unittest.mock import AsyncMock, patch

from gitlab_copilot_agent.coding_engine import SYSTEM_PROMPT, run_coding_task
from tests.conftest import make_settings


@patch("gitlab_copilot_agent.coding_engine.run_copilot_session")
async def test_run_coding_task_delegates_to_copilot_session(
    mock_run_session: AsyncMock,
) -> None:
    mock_run_session.return_value = "Changes completed"

    result = await run_coding_task(
        settings=make_settings(),
        repo_path="/tmp/repo",
        issue_key="PROJ-789",
        summary="Implement feature X",
        description="Add X to the codebase",
    )

    assert result == "Changes completed"
    call_args = mock_run_session.call_args[1]
    assert call_args["system_prompt"] == SYSTEM_PROMPT
    assert "PROJ-789" in call_args["user_prompt"]
    assert "Implement feature X" in call_args["user_prompt"]
