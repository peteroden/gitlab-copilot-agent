"""Tests for TaskExecutor protocol and LocalTaskExecutor."""

from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.task_executor import LocalTaskExecutor, TaskExecutor, TaskParams
from tests.conftest import make_settings

REPO_PATH = "/tmp/test-repo"
REPO_URL = "https://gitlab.example.com/group/project.git"
BRANCH = "main"
SYSTEM_PROMPT = "You are a reviewer."
USER_PROMPT = "Review this code."


def _make_task(**overrides: object) -> TaskParams:
    defaults = {
        "task_type": "review",
        "task_id": "test-1",
        "repo_url": REPO_URL,
        "branch": BRANCH,
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": USER_PROMPT,
        "settings": make_settings(),
        "repo_path": REPO_PATH,
    }
    return TaskParams(**(defaults | overrides))  # type: ignore[arg-type]


class TestTaskParams:
    def test_fields(self) -> None:
        task = _make_task()
        assert task.task_type == "review"
        assert task.task_id == "test-1"
        assert task.repo_path == REPO_PATH

    def test_frozen(self) -> None:
        task = _make_task()
        with pytest.raises(AttributeError):
            task.task_id = "changed"  # type: ignore[misc]

    def test_repo_path_optional(self) -> None:
        task = _make_task(repo_path=None)
        assert task.repo_path is None


class TestLocalTaskExecutor:
    async def test_protocol_compliance(self) -> None:
        assert isinstance(LocalTaskExecutor(), TaskExecutor)

    @patch("gitlab_copilot_agent.copilot_session.run_copilot_session", new_callable=AsyncMock)
    async def test_delegates_to_copilot_session(self, mock_session: AsyncMock) -> None:
        mock_session.return_value = "review result"
        executor = LocalTaskExecutor()
        task = _make_task()
        result = await executor.execute(task)
        assert result == "review result"
        mock_session.assert_awaited_once_with(
            settings=task.settings,
            repo_path=REPO_PATH,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT,
            task_type="review",
        )

    async def test_requires_repo_path(self) -> None:
        executor = LocalTaskExecutor()
        task = _make_task(repo_path=None)
        with pytest.raises(ValueError, match="repo_path"):
            await executor.execute(task)
