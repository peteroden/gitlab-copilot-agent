"""Tests for TaskExecutor protocol and LocalTaskExecutor."""

from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.task_executor import LocalTaskExecutor, TaskExecutor, TaskParams
from tests.conftest import make_settings


def test_local_executor_implements_protocol() -> None:
    """LocalTaskExecutor should be recognized as implementing TaskExecutor."""
    executor = LocalTaskExecutor()
    assert isinstance(executor, TaskExecutor)


@pytest.mark.asyncio
async def test_local_executor_delegates_to_copilot_session() -> None:
    """LocalTaskExecutor.execute() should call run_copilot_session with correct params."""
    settings = make_settings()
    task = TaskParams(
        task_type="review",
        task_id="test-review-123",
        repo_url="/tmp/test-repo",
        branch="feature-branch",
        system_prompt="Test system prompt",
        user_prompt="Test user prompt",
        settings=settings,
    )

    with patch(
        "gitlab_copilot_agent.task_executor.run_copilot_session", new_callable=AsyncMock
    ) as mock_session:
        mock_session.return_value = "Review output"

        executor = LocalTaskExecutor()
        result = await executor.execute(task)

        assert result == "Review output"
        mock_session.assert_called_once_with(
            settings=settings,
            repo_path="/tmp/test-repo",
            system_prompt="Test system prompt",
            user_prompt="Test user prompt",
            task_type="review",
        )


@pytest.mark.asyncio
async def test_local_executor_coding_task() -> None:
    """LocalTaskExecutor should handle coding tasks correctly."""
    settings = make_settings()
    task = TaskParams(
        task_type="coding",
        task_id="test-coding-456",
        repo_url="/tmp/code-repo",
        branch="main",
        system_prompt="Coding system prompt",
        user_prompt="Implement feature X",
        settings=settings,
    )

    with patch(
        "gitlab_copilot_agent.task_executor.run_copilot_session", new_callable=AsyncMock
    ) as mock_session:
        mock_session.return_value = "Coding result"

        executor = LocalTaskExecutor()
        result = await executor.execute(task)

        assert result == "Coding result"
        assert mock_session.call_args.kwargs["task_type"] == "coding"


@pytest.mark.asyncio
async def test_task_params_immutable() -> None:
    """TaskParams should be frozen/immutable."""
    settings = make_settings()
    task = TaskParams(
        task_type="review",
        task_id="immutable-test",
        repo_url="/tmp/repo",
        branch="main",
        system_prompt="System",
        user_prompt="User",
        settings=settings,
    )

    # Attempting to modify should raise FrozenInstanceError or AttributeError
    with pytest.raises((FrozenInstanceError, AttributeError)):
        task.task_id = "new-id"  # type: ignore[misc]
