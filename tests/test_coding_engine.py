"""Tests for the coding engine."""

from unittest.mock import AsyncMock

from gitlab_copilot_agent.coding_engine import CODING_SYSTEM_PROMPT, run_coding_task
from tests.conftest import EXAMPLE_CLONE_URL, make_settings


async def test_run_coding_task_delegates_to_executor() -> None:
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "Changes completed"

    result = await run_coding_task(
        executor=mock_executor,
        settings=make_settings(),
        repo_path="/tmp/repo",
        repo_url=EXAMPLE_CLONE_URL,
        branch="agent/proj-789",
        issue_key="PROJ-789",
        summary="Implement feature X",
        description="Add X to the codebase",
    )

    assert result == "Changes completed"
    task = mock_executor.execute.call_args[0][0]
    assert task.system_prompt == CODING_SYSTEM_PROMPT
    assert "PROJ-789" in task.user_prompt
    assert "Implement feature X" in task.user_prompt
