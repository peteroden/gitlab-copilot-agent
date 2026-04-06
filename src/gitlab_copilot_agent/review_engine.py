"""Copilot review engine — runs an agent review session on an MR."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.prompt_defaults import DEFAULT_REVIEW_PROMPT, get_prompt
from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams, TaskResult

if TYPE_CHECKING:
    from gitlab_copilot_agent.discussion_models import DiscussionHistory

log = structlog.get_logger()

# Max characters of diff to include in the prompt.  Beyond this the diff is
# truncated and the LLM is told to run git diff for the full picture.
MAX_DIFF_CHARS = 120_000

_SEVERITY_PREFIX_RE = re.compile(r"^\*\*\[(?:ERROR|WARNING|INFO)\]\*\*\s*")
_SUGGESTION_BLOCK_RE = re.compile(r"\n\n```suggestion.*?```", re.DOTALL)
# ↑ Formats coupled with comment_poster.py:89-93 — update in lockstep.

_PRIOR_FEEDBACK_RULES = """\
Rules:
- Do NOT generate any comment that covers the same issue as any item \
in Agent's Prior Feedback, even if line numbers have shifted.
- If the code has changed but the underlying issue remains, the prior \
feedback still applies — do not re-comment.\
"""

REVIEW_SYSTEM_PROMPT = DEFAULT_REVIEW_PROMPT


class ReviewRequest(BaseModel):
    """Minimal info the agent needs to perform a review."""

    model_config = ConfigDict(frozen=True)
    title: str = Field(description="MR title")
    description: str | None = Field(description="MR description")
    source_branch: str = Field(description="Source branch name")
    target_branch: str = Field(description="Target branch name")


def _strip_comment_formatting(body: str) -> str:
    """Remove agent-added severity prefix and suggestion blocks from a comment."""
    body = _SEVERITY_PREFIX_RE.sub("", body)
    body = _SUGGESTION_BLOCK_RE.sub("", body)
    return body.strip()


def _format_prior_feedback(history: DiscussionHistory) -> str:
    """Render the agent's unresolved inline comments as a prompt section.

    Returns the complete section (header + grouped comments + rules) or an
    empty string when no qualifying comments exist.
    """
    comments_by_file: dict[str, list[tuple[int | None, str]]] = defaultdict(list)

    for disc in history.discussions:
        if disc.is_resolved or not disc.is_inline:
            continue
        if not disc.notes:
            continue
        first_note = disc.notes[0]
        if first_note.author_id != history.agent.user_id:
            continue

        position = first_note.position or {}
        raw_path = position.get("new_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        file_path = raw_path

        raw_line = position.get("new_line")
        line_num: int | None = None
        if isinstance(raw_line, int):
            line_num = raw_line
        elif isinstance(raw_line, str):
            try:
                line_num = int(raw_line)
            except ValueError:
                line_num = None
        body = _strip_comment_formatting(first_note.body)
        comments_by_file[file_path].append((line_num, body))

    if not comments_by_file:
        return ""

    lines = ["## Agent's Prior Feedback (Unresolved)\n"]
    for file_path in sorted(comments_by_file):
        lines.append(f"### {file_path}")
        for line_num, body in sorted(
            comments_by_file[file_path], key=lambda c: (c[0] is None, c[0] or 0)
        ):
            prefix = f"Line {line_num}" if line_num is not None else "General"
            lines.append(f"- {prefix}: {body}")
        lines.append("")  # blank line between files

    lines.append(_PRIOR_FEEDBACK_RULES)
    return "\n".join(lines)


def build_review_prompt(
    req: ReviewRequest,
    diff_text: str | None = None,
    discussion_history: DiscussionHistory | None = None,
) -> str:
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

    if discussion_history:
        prior_section = _format_prior_feedback(discussion_history)
        if prior_section:
            prompt += f"\n\n{prior_section}"
    return prompt


async def run_review(
    executor: TaskExecutor,
    settings: Settings,
    repo_path: str,
    repo_url: str,
    review_request: ReviewRequest,
    diff_text: str | None = None,
    discussion_history: DiscussionHistory | None = None,
) -> TaskResult:
    """Run a Copilot agent review and return the structured result."""
    task = TaskParams(
        task_type="review",
        task_id=f"review-{review_request.source_branch}",
        repo_url=repo_url,
        branch=review_request.source_branch,
        system_prompt=get_prompt(settings, "review"),
        user_prompt=build_review_prompt(review_request, diff_text, discussion_history),
        settings=settings,
        repo_path=repo_path,
    )
    return await executor.execute(task)
