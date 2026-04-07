"""SHA marker utilities for incremental MR review.

Embeds and extracts a hidden HTML comment in overview notes to track
the last-reviewed commit SHA.  See ADR-0009.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gitlab_copilot_agent.discussion_models import DiscussionHistory

_SHA_MARKER_RE = re.compile(r"<!-- mr-review-agent: last_reviewed_sha=([a-f0-9]{7,40}) -->")


def extract_last_reviewed_sha(
    discussion_history: DiscussionHistory | None,
) -> str | None:
    """Extract the last-reviewed SHA from the agent's most recent summary note.

    Scans overview (non-inline) notes authored by the agent in reverse
    chronological order for the hidden SHA marker.
    """
    if not discussion_history:
        return None

    for disc in reversed(discussion_history.discussions):
        if disc.is_inline or disc.is_resolved:
            continue
        for note in reversed(disc.notes):
            if note.author_id != discussion_history.agent.user_id:
                continue
            match = _SHA_MARKER_RE.search(note.body)
            if match:
                return match.group(1)
    return None


def format_sha_marker(head_sha: str) -> str:
    """Generate the hidden SHA marker for embedding in summary notes."""
    return f"<!-- mr-review-agent: last_reviewed_sha={head_sha} -->"
