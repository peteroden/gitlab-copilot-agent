"""TaskExecutor protocol and LocalTaskExecutor implementation."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.config import Settings


class TaskParams(BaseModel):
    """Parameters for a Copilot task execution."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    task_type: Literal["review", "coding"] = Field(description="Type of task to execute")
    task_id: str = Field(description="Unique identifier for this task")
    repo_url: str = Field(description="Git clone URL for the repository")
    branch: str = Field(description="Branch to review or work on")
    system_prompt: str = Field(description="System prompt for the Copilot session")
    user_prompt: str = Field(description="User prompt for the Copilot session")
    settings: Settings = Field(description="Application settings")
    repo_path: str | None = Field(default=None, description="Local path to cloned repo")


@runtime_checkable
class TaskExecutor(Protocol):
    """Execute a Copilot session and return the result."""

    async def execute(self, task: TaskParams) -> str: ...


class LocalTaskExecutor:
    """Runs Copilot sessions directly in-process.

    Expects ``task.repo_path`` to be set to a local checkout.
    KubernetesTaskExecutor (future) uses ``task.repo_url`` instead.
    """

    async def execute(self, task: TaskParams) -> str:
        if not task.repo_path:
            raise ValueError("LocalTaskExecutor requires task.repo_path")

        from gitlab_copilot_agent.copilot_session import run_copilot_session

        return await run_copilot_session(
            settings=task.settings,
            repo_path=task.repo_path,
            system_prompt=task.system_prompt,
            user_prompt=task.user_prompt,
            task_type=task.task_type,
        )
