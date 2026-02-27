"""Copilot review engine — runs an agent review session on an MR."""

import structlog
from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.prompt_defaults import DEFAULT_REVIEW_PROMPT, get_prompt
from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams, TaskResult

log = structlog.get_logger()

# Max characters of diff to include in the prompt.  Beyond this the diff is
# truncated and the LLM is told to run git diff for the full picture.
MAX_DIFF_CHARS = 120_000

REVIEW_SYSTEM_PROMPT = DEFAULT_REVIEW_PROMPT


class ReviewRequest(BaseModel):
    """Minimal info the agent needs to perform a review."""

    model_config = ConfigDict(frozen=True)
    title: str = Field(description="MR title")
    description: str | None = Field(description="MR description")
    source_branch: str = Field(description="Source branch name")
    target_branch: str = Field(description="Target branch name")


def build_review_prompt(req: ReviewRequest, diff_text: str | None = None) -> str:
    """Build the user prompt — includes the diff directly when available."""
    prompt = (
        f"## Merge Request\n"
        f"**Title:** {req.title}\n"
        f"**Description:** {req.description or '(none)'}\n"
        f"**Source branch:** {req.source_branch}\n"
        f"**Target branch:** {req.target_branch}\n\n"
    )
    if diff_text:
        if len(diff_text) > MAX_DIFF_CHARS:
            log.warning(
                "diff_truncated",
                original_len=len(diff_text),
                max_len=MAX_DIFF_CHARS,
            )
            diff_text = diff_text[:MAX_DIFF_CHARS] + "\n... (diff truncated)"
        prompt += f"## Diff\n\n```diff\n{diff_text}\n```\n\n"
        prompt += "Review ONLY the changes shown in the diff above."
    else:
        prompt += (
            f"Run `git diff {req.target_branch}...{req.source_branch}` to see "
            f"the changes, then read relevant files for context."
        )
    return prompt


async def run_review(
    executor: TaskExecutor,
    settings: Settings,
    repo_path: str,
    repo_url: str,
    review_request: ReviewRequest,
    diff_text: str | None = None,
) -> TaskResult:
    """Run a Copilot agent review and return the structured result."""
    task = TaskParams(
        task_type="review",
        task_id=f"review-{review_request.source_branch}",
        repo_url=repo_url,
        branch=review_request.source_branch,
        system_prompt=get_prompt(settings, "review"),
        user_prompt=build_review_prompt(review_request, diff_text),
        settings=settings,
        repo_path=repo_path,
    )
    return await executor.execute(task)
