"""TaskExecutor protocol and implementations.

Abstracts task execution from dispatch — callers use executor.execute(task)
regardless of whether it runs locally or via k8s Job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.copilot_session import run_copilot_session


@dataclass(frozen=True)
class TaskParams:
    """Parameters for a Copilot task execution.
    
    For LocalTaskExecutor, repo_url should be a local filesystem path to an
    already-cloned repository. For KubernetesTaskExecutor (future), it's a URL.
    """

    task_type: Literal["review", "coding"]
    task_id: str
    repo_url: str  # Local path for local executor, URL for k8s executor
    branch: str
    system_prompt: str
    user_prompt: str
    settings: Settings


@runtime_checkable
class TaskExecutor(Protocol):
    """Execute a Copilot session and return the result."""

    async def execute(self, task: TaskParams) -> str:
        """Execute a task and return the agent's response text."""
        ...


class LocalTaskExecutor:
    """Runs Copilot session directly in-process.

    Wraps existing run_copilot_session() — no k8s Job dispatch.
    """

    async def execute(self, task: TaskParams) -> str:
        """Execute task locally by delegating to run_copilot_session."""
        return await run_copilot_session(
            settings=task.settings,
            repo_path=task.repo_url,  # Expected to be a local filesystem path
            system_prompt=task.system_prompt,
            user_prompt=task.user_prompt,
            task_type=task.task_type,
        )
