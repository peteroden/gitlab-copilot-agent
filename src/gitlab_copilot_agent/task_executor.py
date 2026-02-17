"""TaskExecutor protocol and LocalTaskExecutor implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings


@dataclass(frozen=True)
class TaskParams:
    """Parameters for a Copilot task execution."""

    task_type: Literal["review", "coding"]
    task_id: str
    repo_url: str
    branch: str
    system_prompt: str
    user_prompt: str
    settings: Settings
    repo_path: str | None = None


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
