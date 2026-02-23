"""Copilot review engine — runs an agent review session on an MR."""

import structlog
from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams, TaskResult

log = structlog.get_logger()

# Max characters of diff to include in the prompt.  Beyond this the diff is
# truncated and the LLM is told to run git diff for the full picture.
MAX_DIFF_CHARS = 120_000

SYSTEM_PROMPT = """\
You are a senior code reviewer. Review the merge request diff thoroughly.

Focus on:
- Bugs, logic errors, and edge cases
- Security vulnerabilities (OWASP Top 10)
- Performance issues
- Code clarity and maintainability

IMPORTANT: The "line" field in your output MUST be the line number as shown in
the NEW version of the file (the right-hand side of the diff). Use the line
numbers from the `+` side of the `git diff` output. Double-check each line
number by counting from the hunk header `@@ ... +START,COUNT @@`.
Use the FULL file path as shown in the diff (e.g. `src/demo_app/search.py`,
not just `search.py`).

CRITICAL: Only comment on files and lines that are PART OF THE DIFF provided
in the user message. Do not review or comment on files that are not in the diff.

Output your review as a JSON array:
```json
[
  {
    "file": "src/full/path/to/file.py",
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
  Suggestions MUST be self-contained: if the fix requires a new import,
  mention the needed import in the comment text (suggestions can only
  replace contiguous lines, so distant changes like imports cannot be
  included in the suggestion itself).
- "suggestion_start_offset": Lines ABOVE the commented line to replace (default 0).
- "suggestion_end_offset": Lines BELOW the commented line to replace (default 0).
  For example, to replace just the commented line, use offsets 0, 0.
  To replace a 3-line block (1 above + commented + 1 below), use 1, 1.

After the JSON array, add a brief summary paragraph.
If the code looks good, return an empty array and say so in the summary.
"""


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
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_review_prompt(review_request, diff_text),
        settings=settings,
        repo_path=repo_path,
    )
    return await executor.execute(task)
