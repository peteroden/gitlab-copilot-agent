"""Copilot review engine — runs an agent review session on an MR."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.prompt_defaults import get_prompt
from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams, TaskResult

if TYPE_CHECKING:
    from gitlab_copilot_agent.discussion_models import (
        Discussion,
        DiscussionHistory,
        DiscussionNote,
    )

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

_SUPPRESSED_FEEDBACK_RULES = """\
Rules:
- Do NOT re-raise, reference, or generate any comment covering the same \
issue as any item listed below, even if the code pattern persists.
- These items were reviewed by a human and intentionally resolved or \
dismissed — respect the developer's decision.\
"""

_DISMISSAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bwon'?t\s+fix\b",
        r"\bintentional\b",
        r"\bby\s+design\b",
        r"\bnot\s+a\s+bug\b",
        r"\bfalse\s+positive\b",
        r"\bnot\s+(?:an?\s+)?issue\b",
        r"\bacceptable?\s+risk\b",
        r"\bwontfix\b",
    ]
]


def _is_human_resolved(disc: Discussion, agent_user_id: int) -> bool:
    """True if a human (not the agent) resolved this discussion."""
    if not disc.is_resolved:
        return False
    for note in disc.notes:
        if note.resolved_by_id is not None and note.resolved_by_id != agent_user_id:
            return True
    return False


def _is_dismissed(disc: Discussion, agent_user_id: int) -> bool:
    """True if a developer replied with a dismissal phrase."""
    for note in disc.notes:
        if note.author_id == agent_user_id:
            continue
        if any(p.search(note.body) for p in _DISMISSAL_PATTERNS):
            return True
    return False


_RESOLUTION_EVAL_INSTRUCTIONS = """\
## Resolution Evaluation

For each item in "Agent's Prior Feedback (Unresolved)" above, evaluate whether
the new diff addresses the feedback. Include a "resolutions" array in your JSON
output with one entry per prior feedback item:

```json
{
  "resolutions": [
    {
      "discussion_id": "<discussion_id from prior feedback>",
      "status": "resolved|not_addressed|partial",
      "message": "Brief explanation of why the feedback is/isn't addressed"
    }
  ]
}
```

Status values:
- "resolved": The code change fully addresses the feedback
- "not_addressed": The feedback issue remains in the code
- "partial": Some aspects addressed but others remain (always explain what's left)

If there is no prior feedback, return an empty resolutions array.\
"""


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


def _format_prior_feedback(history: DiscussionHistory, current_head_sha: str = "") -> str:
    """Render the agent's unresolved inline comments as a prompt section.

    Returns the complete section (header + grouped comments + rules) or an
    empty string when no qualifying comments exist.
    """
    comments_by_file: dict[str, list[tuple[int | None, str, str, dict[str, object] | None]]] = (
        defaultdict(list)
    )

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
        comments_by_file[file_path].append(
            (line_num, body, disc.discussion_id, first_note.position)
        )

    if not comments_by_file:
        return ""

    lines = ["## Agent's Prior Feedback (Unresolved)\n"]
    for file_path in sorted(comments_by_file):
        lines.append(f"### {file_path}")
        for line_num, body, disc_id, position in sorted(
            comments_by_file[file_path], key=lambda c: (c[0] is None, c[0] or 0)
        ):
            position_head = position.get("head_sha") if position else None
            is_outdated = (
                current_head_sha
                and isinstance(position_head, str)
                and position_head != current_head_sha
            )
            prefix = f"Line {line_num}" if line_num is not None else "General"
            if is_outdated:
                prefix += " (outdated position — line may have shifted)"
            lines.append(f"- {prefix}: {body} [discussion: {disc_id}]")
        lines.append("")  # blank line between files

    lines.append(_PRIOR_FEEDBACK_RULES)
    return "\n".join(lines)


def _file_line(note: DiscussionNote) -> str:
    """Format a note's file and line info for display."""
    position = note.position or {}
    raw_path = position.get("new_path")
    file_path = raw_path if isinstance(raw_path, str) and raw_path.strip() else "unknown"
    raw_line = position.get("new_line")
    if isinstance(raw_line, int):
        return f"{file_path}:{raw_line}"
    return file_path


def _format_suppressed_feedback(history: DiscussionHistory) -> str:
    """Render human-resolved and dismissed items as a suppressed feedback prompt section.

    Returns the complete section (header + items + rules) or an empty string
    when no suppressed items exist.
    """
    suppressed: list[str] = []
    for disc in history.discussions:
        if not disc.is_inline:
            continue
        if not disc.notes:
            continue
        first = disc.notes[0]
        if first.author_id != history.agent.user_id:
            continue
        if _is_human_resolved(disc, history.agent.user_id):
            body = _strip_comment_formatting(first.body)
            suppressed.append(f"- {_file_line(first)}: {body} [MANUALLY RESOLVED]")
        elif _is_dismissed(disc, history.agent.user_id):
            body = _strip_comment_formatting(first.body)
            suppressed.append(f"- {_file_line(first)}: {body} [DISMISSED]")

    if not suppressed:
        return ""

    lines = ["## Suppressed Feedback (Do Not Re-Raise)\n"]
    lines.extend(suppressed)
    lines.append("")
    lines.append(_SUPPRESSED_FEEDBACK_RULES)
    return "\n".join(lines)


def build_review_prompt(
    req: ReviewRequest,
    diff_text: str | None = None,
    discussion_history: DiscussionHistory | None = None,
    is_incremental: bool = False,
    head_sha: str = "",
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
        if is_incremental:
            prompt += (
                "## Incremental Diff (changes since last review)\n\n"
                "You are reviewing ONLY the new changes since the last review. "
                "Prior feedback on unchanged code is listed separately below.\n\n"
            )
        else:
            prompt += "## Diff\n\n"
        prompt += f"```diff\n{diff_text}\n```\n\n"
        prompt += "Review ONLY the changes shown in the diff above."
    else:
        prompt += (
            f"Run `git diff {req.target_branch}...{req.source_branch}` to see "
            f"the changes, then read relevant files for context."
        )

    if discussion_history:
        prior_section = _format_prior_feedback(discussion_history, current_head_sha=head_sha)
        if prior_section:
            prompt += f"\n\n{prior_section}"
            prompt += f"\n\n{_RESOLUTION_EVAL_INSTRUCTIONS}"
        suppressed_section = _format_suppressed_feedback(discussion_history)
        if suppressed_section:
            prompt += f"\n\n{suppressed_section}"
    return prompt


async def run_review(
    executor: TaskExecutor,
    settings: Settings,
    repo_path: str,
    repo_url: str,
    review_request: ReviewRequest,
    diff_text: str | None = None,
    discussion_history: DiscussionHistory | None = None,
    head_sha: str = "",
    is_incremental: bool = False,
) -> TaskResult:
    """Run a Copilot agent review and return the structured result."""
    import hashlib

    task_id = f"review-{review_request.source_branch}"
    if head_sha:
        task_id = f"{task_id}-{head_sha[:12]}"

    user_prompt = build_review_prompt(
        review_request,
        diff_text,
        discussion_history,
        is_incremental=is_incremental,
        head_sha=head_sha,
    )
    log.debug(
        "review_prompt_built",
        prompt_length=len(user_prompt),
        prompt_hash=hashlib.sha256(user_prompt.encode()).hexdigest()[:16],
        is_incremental=is_incremental,
    )

    task = TaskParams(
        task_type="review",
        task_id=task_id,
        repo_url=repo_url,
        branch=review_request.source_branch,
        system_prompt=get_prompt(settings, "review"),
        user_prompt=user_prompt,
        settings=settings,
        repo_path=repo_path,
    )
    result = await executor.execute(task)

    log.debug(
        "review_raw_response",
        response_length=len(result.summary),
        response_preview=result.summary[:500],
    )
    return result
