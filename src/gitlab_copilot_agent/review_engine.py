"""Copilot review engine — runs an agent review session on an MR."""

from dataclasses import dataclass

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams

SYSTEM_PROMPT = """\
You are a senior code reviewer. Review the merge request diff thoroughly.

Focus on:
- Bugs, logic errors, and edge cases
- Security vulnerabilities (OWASP Top 10)
- Performance issues
- Code clarity and maintainability

You have access to the full repository via built-in file tools. Use them to
read source files and understand context beyond the diff.

Output your review as a JSON array:
```json
[
  {
    "file": "path/to/file",
    "line": 42,
    "severity": "error|warning|info",
    "comment": "Description of the issue",
    "suggestion": "replacement code for the line(s)",
    "suggestion_start_offset": 0,
    "suggestion_end_offset": 0
  }
]
```

Suggestion fields:
- "suggestion": The replacement code. Include ONLY when you can provide a
  concrete, unambiguous fix. Omit for observations or questions.
- "suggestion_start_offset": Lines ABOVE the commented line to replace (default 0).
- "suggestion_end_offset": Lines BELOW the commented line to replace (default 0).
  For example, to replace just the commented line, use offsets 0, 0.
  To replace a 3-line block (1 above + commented + 1 below), use 1, 1.

After the JSON array, add a brief summary paragraph.
If the code looks good, return an empty array and say so in the summary.
"""


@dataclass(frozen=True)
class ReviewRequest:
    """Minimal info the agent needs to perform a review."""

    title: str
    description: str | None
    source_branch: str
    target_branch: str


def build_review_prompt(req: ReviewRequest) -> str:
    """Build the user prompt — the agent uses git diff and file tools."""
    return (
        f"## Merge Request\n"
        f"**Title:** {req.title}\n"
        f"**Description:** {req.description or '(none)'}\n"
        f"**Source branch:** {req.source_branch}\n"
        f"**Target branch:** {req.target_branch}\n\n"
        f"Review this merge request. Run "
        f"`git diff {req.target_branch}...{req.source_branch}` to see "
        f"the changes, then read relevant files for context."
    )


async def run_review(
    executor: TaskExecutor,
    settings: Settings,
    repo_path: str,
    repo_url: str,
    review_request: ReviewRequest,
) -> str:
    """Run a Copilot agent review and return the raw response text."""
    task = TaskParams(
        task_type="review",
        task_id=f"review-{review_request.source_branch}",
        repo_url=repo_url,
        branch=review_request.source_branch,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_review_prompt(review_request),
        settings=settings,
        repo_path=repo_path,
    )
    return await executor.execute(task)
