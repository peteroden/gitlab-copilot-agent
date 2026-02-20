"""TaskExecutor protocol, result types, and LocalTaskExecutor implementation."""

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


class ReviewResult(BaseModel):
    """Result from a review task — text summary only."""

    model_config = ConfigDict(frozen=True)
    result_type: Literal["review"] = "review"
    summary: str = Field(description="Raw review output from Copilot")


class CodingResult(BaseModel):
    """Result from a coding task — summary plus optional diff for k8s executor."""

    model_config = ConfigDict(frozen=True)
    result_type: Literal["coding"] = "coding"
    summary: str = Field(description="Summary of changes from Copilot")
    patch: str = Field(default="", description="Unified diff (git diff --cached --binary)")
    base_sha: str = Field(default="", description="Commit SHA the patch is based on")


TaskResult = ReviewResult | CodingResult


@runtime_checkable
class TaskExecutor(Protocol):
    """Execute a Copilot session and return a structured result."""

    async def execute(self, task: TaskParams) -> TaskResult: ...


class LocalTaskExecutor:
    """Runs Copilot sessions directly in-process.

    Expects ``task.repo_path`` to be set to a local checkout.
    Returns a result with no patch — files are already on disk.
    """

    async def execute(self, task: TaskParams) -> TaskResult:
        if not task.repo_path:
            raise ValueError("LocalTaskExecutor requires task.repo_path")

        from gitlab_copilot_agent.copilot_session import run_copilot_session

        summary = await run_copilot_session(
            settings=task.settings,
            repo_path=task.repo_path,
            system_prompt=task.system_prompt,
            user_prompt=task.user_prompt,
            task_type=task.task_type,
        )
        if task.task_type == "review":
            return ReviewResult(summary=summary)
        return CodingResult(summary=summary)
