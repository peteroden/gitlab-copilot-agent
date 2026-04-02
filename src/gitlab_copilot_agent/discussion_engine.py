"""Discussion engine — builds prompts and runs LLM sessions for thread interactions.

Handles all @mention and thread-reply interactions: Q&A, coding requests,
and resolution signals.  Uses the discussion prompt persona.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams, TaskResult

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.discussion_models import Discussion, DiscussionHistory
    from gitlab_copilot_agent.gitlab_client import MRDetails

log = structlog.get_logger()

# Max characters of diff to include in the prompt.
MAX_DIFF_CHARS = 80_000

# Max number of "other discussions" to include for broader context.
MAX_OTHER_DISCUSSIONS = 5

# Max characters to show per summary note in other-discussions section.
MAX_OTHER_NOTE_CHARS = 100


class DiscussionResponse(BaseModel):
    """Parsed response from the discussion LLM session.

    The reply text is posted to the thread.  If the LLM made code changes,
    ``has_code_changes`` is True and the caller should commit/push (the
    executor already captured the file edits via ``apply_coding_result``).
    """

    model_config = ConfigDict(frozen=True)

    reply: str = Field(description="Reply text to post in the thread")
    has_code_changes: bool = Field(
        default=False, description="True if the LLM output contained a files_changed JSON block"
    )


def build_discussion_prompt(
    mr_details: MRDetails,
    discussion_history: DiscussionHistory,
    triggering_discussion: Discussion,
) -> str:
    """Build the user prompt for a discussion interaction.

    Includes: MR metadata, the triggering thread (full conversation),
    the diff, and the broader discussion context.
    """
    # MR metadata
    prompt = (
        f"## Merge Request\n"
        f"**Title:** {mr_details.title}\n"
        f"**Description:** {mr_details.description or '(none)'}\n\n"
    )

    # The triggering thread — this is the conversation the developer is in
    prompt += "## Current Thread\n\n"
    for note in triggering_discussion.notes:
        role = (
            "Agent" if note.author_id == discussion_history.agent.user_id else note.author_username
        )
        prompt += f"**{role}** ({note.created_at}):\n{note.body}\n\n"

    # Diff context
    diff_text = "\n".join(
        f"--- a/{c.old_path}\n+++ b/{c.new_path}\n{c.diff}" for c in mr_details.changes
    )
    if diff_text:
        if len(diff_text) > MAX_DIFF_CHARS:
            diff_text = diff_text[:MAX_DIFF_CHARS] + "\n... (diff truncated)"
        prompt += f"## Diff\n\n```diff\n{diff_text}\n```\n\n"

    # Other discussions for broader context (summarized)
    other = [
        d
        for d in discussion_history.discussions
        if d.discussion_id != triggering_discussion.discussion_id
    ]
    if other:
        prompt += f"## Other Active Discussions ({len(other)} threads)\n\n"
        for disc in other[:MAX_OTHER_DISCUSSIONS]:
            first_note = disc.notes[0] if disc.notes else None
            if first_note:
                status = "resolved" if disc.is_resolved else "open"
                prompt += f"- [{status}] {first_note.body[:MAX_OTHER_NOTE_CHARS]}...\n"
        prompt += "\n"

    prompt += "Respond to the latest message in the Current Thread above."
    return prompt


def parse_discussion_response(raw: str) -> DiscussionResponse:
    """Parse the LLM's discussion response.

    If the output ends with a ``files_changed`` JSON block (same format as
    the coding prompt), the reply is the text before the block and
    ``has_code_changes`` is True.  Otherwise the entire output is the reply.
    """
    # Check for trailing JSON block with files_changed (coding prompt format)
    json_match = re.search(r"```json\s*\n(\{.*?\})\s*\n```\s*$", raw, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if "files_changed" in data:
                reply = raw[: json_match.start()].strip()
                if not reply:
                    reply = data.get("summary", "Changes applied.")
                return DiscussionResponse(reply=reply, has_code_changes=True)
        except (json.JSONDecodeError, ValidationError):
            pass

    return DiscussionResponse(reply=raw.strip())


async def run_discussion(
    executor: TaskExecutor,
    settings: Settings,
    repo_path: str,
    repo_url: str,
    system_prompt: str,
    user_prompt: str,
    source_branch: str,
) -> TaskResult:
    """Run a discussion LLM session and return the result.

    The caller is responsible for resolving the system prompt (e.g. via
    ``get_prompt``).  This keeps the engine decoupled from prompt config.
    """
    task = TaskParams(
        task_type="coding",  # use coding task type for repo access
        task_id=f"discussion-{source_branch}",
        repo_url=repo_url,
        branch=source_branch,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        settings=settings,
        repo_path=repo_path,
    )
    return await executor.execute(task)
