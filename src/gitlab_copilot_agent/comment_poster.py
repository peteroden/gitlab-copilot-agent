"""Post review comments to GitLab MR as inline discussions and summary."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import gitlab as gl

    from gitlab_copilot_agent.comment_parser import ParsedReview
    from gitlab_copilot_agent.gitlab_client import MRChange, MRDiffRef

log = logging.getLogger(__name__)


def _parse_hunk_lines(diff: str, new_path: str) -> set[tuple[str, int]]:
    """Extract valid new_line positions from unified diff hunks.

    Returns a set of (file_path, new_line_number) tuples that can receive inline comments.
    Only lines present in the new file are valid: added lines and context lines.
    """
    valid_positions: set[tuple[str, int]] = set()
    # Unified diff hunk header: @@ -old_start,old_count +new_start,new_count @@
    hunk_pattern = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", re.MULTILINE)

    for match in hunk_pattern.finditer(diff):
        new_line = int(match.group(1))
        hunk_start = match.end()
        # Find next hunk or end of diff
        next_match = hunk_pattern.search(diff, hunk_start)
        hunk_end = next_match.start() if next_match else len(diff)
        hunk_body = diff[hunk_start:hunk_end]

        for line in hunk_body.splitlines():
            if not line:
                continue
            prefix = line[0] if line else ""
            # ' ' = context (in both old and new)
            # '+' = added (only in new)
            # '-' = removed (only in old)
            if prefix in (" ", "+"):
                # These lines exist in the new file
                valid_positions.add((new_path, new_line))
                new_line += 1
            elif prefix == "-":
                # Removed lines don't advance new_line
                pass
            else:
                # Metadata or continuation; don't advance
                pass

    return valid_positions


def _is_valid_position(file: str, line: int, valid_positions: set[tuple[str, int]]) -> bool:
    """Check if a comment position (file, line) is valid in the MR diff."""
    return (file, line) in valid_positions


async def post_review(
    gitlab_client: gl.Gitlab,
    project_id: int,
    mr_iid: int,
    diff_refs: MRDiffRef,
    review: ParsedReview,
    changes: list[MRChange],
) -> None:
    """Post inline comments and summary note to a GitLab MR.

    Validates comment positions against MR diff before attempting inline discussions.
    Invalid positions are posted as fallback notes with file:line context.
    """

    def _post() -> None:
        project = gitlab_client.projects.get(project_id)
        mr = project.mergerequests.get(mr_iid)

        # Precompute valid positions once for all comments
        valid_positions: set[tuple[str, int]] = set()
        for change in changes:
            valid_positions |= _parse_hunk_lines(change.diff, change.new_path)

        for c in review.comments:
            body = f"**[{c.severity.upper()}]** {c.comment}"
            if c.suggestion is not None:
                start = c.suggestion_start_offset
                end = c.suggestion_end_offset
                body += f"\n\n```suggestion:-{start}+{end}\n{c.suggestion}\n```"

            # Validate position before attempting inline comment
            if not _is_valid_position(c.file, c.line, valid_positions):
                log.warning(
                    "Comment position not in diff, using fallback note for %s:%d",
                    c.file,
                    c.line,
                )
                try:
                    mr.notes.create({"body": f"{body}\n\n`{c.file}:{c.line}`"})
                except Exception:
                    log.warning("Fallback note failed for %s:%d", c.file, c.line, exc_info=True)
                continue

            # Position is valid, attempt inline comment
            try:
                mr.discussions.create(
                    {
                        "body": body,
                        "position": {
                            "base_sha": diff_refs.base_sha,
                            "start_sha": diff_refs.start_sha,
                            "head_sha": diff_refs.head_sha,
                            "position_type": "text",
                            "old_path": c.file,
                            "new_path": c.file,
                            "new_line": c.line,
                        },
                    }
                )
            except Exception:
                log.warning("Inline comment failed for %s:%d", c.file, c.line, exc_info=True)
                try:
                    mr.notes.create({"body": f"{body}\n\n`{c.file}:{c.line}`"})
                except Exception:
                    log.warning(
                        "Fallback note also failed for %s:%d",
                        c.file,
                        c.line,
                        exc_info=True,
                    )

        mr.notes.create({"body": f"## Code Review Summary\n\n{review.summary}"})

    await asyncio.to_thread(_post)
