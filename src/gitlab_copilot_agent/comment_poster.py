"""Post review comments to GitLab MR as inline discussions and summary."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.incremental import format_sha_marker

if TYPE_CHECKING:
    import gitlab as gl

    from gitlab_copilot_agent.comment_parser import ParsedReview, Resolution
    from gitlab_copilot_agent.gitlab_client import MRChange, MRDiffRef
    from gitlab_copilot_agent.mapping_models import ResolutionBehavior

log = structlog.get_logger()


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


def _handle_resolutions(
    mr: object,  # gitlab MR object
    resolutions: list[Resolution],
    resolution_behavior: ResolutionBehavior,
    allowed_discussion_ids: frozenset[str] = frozenset(),
) -> int:
    """Process resolutions per configured behavior. Returns count of resolved."""
    if resolution_behavior == "off" or not resolutions:
        return 0

    resolved_count = 0
    for r in resolutions:
        if r.status == "not_addressed":
            continue
        if r.discussion_id not in allowed_discussion_ids:
            log.warning(
                "resolution_skipped_unknown_discussion",
                discussion_id=r.discussion_id,
            )
            continue
        try:
            disc = mr.discussions.get(r.discussion_id)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownVariableType, reportUnknownMemberType]

            if resolution_behavior == "auto-resolve" and r.status == "resolved":
                disc.notes.create({"body": f"✅ {r.message}"})  # pyright: ignore[reportUnknownMemberType]
                disc.resolved = True  # pyright: ignore[reportAttributeAccessIssue]
                disc.save()  # pyright: ignore[reportUnknownMemberType]
                resolved_count += 1
            elif r.status in ("resolved", "partial"):
                prefix = "✅" if r.status == "resolved" else "⚠️"
                disc.notes.create({"body": f"{prefix} {r.message}"})  # pyright: ignore[reportUnknownMemberType]
        except Exception:
            log.warning(
                "resolution_action_failed",
                discussion_id=r.discussion_id,
                exc_info=True,
            )

    return resolved_count


async def post_review(
    gitlab_client: gl.Gitlab,
    project_id: int,
    mr_iid: int,
    diff_refs: MRDiffRef,
    review: ParsedReview,
    changes: list[MRChange],
    resolution_behavior: ResolutionBehavior = "suggest",
    allowed_discussion_ids: frozenset[str] = frozenset(),
    head_sha: str = "",
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
        diff_files: set[str] = set()
        for change in changes:
            diff_files.add(change.new_path)
            valid_positions |= _parse_hunk_lines(change.diff, change.new_path)

        for c in review.comments:
            body = f"**[{c.severity.upper()}]** {c.comment}"
            if c.suggestion is not None:
                start = c.suggestion_start_offset
                end = c.suggestion_end_offset
                body += f"\n\n```suggestion:-{start}+{end}\n{c.suggestion}\n```"

            # Skip comments on files not in the diff entirely
            if c.file not in diff_files:
                log.info("comment_skipped_not_in_diff", file=c.file, line=c.line)
                continue

            # Validate position before attempting inline comment
            if not _is_valid_position(c.file, c.line, valid_positions):
                log.warning(
                    "comment_position_invalid",
                    file=c.file,
                    line=c.line,
                )
                try:
                    mr.notes.create({"body": f"{body}\n\n`{c.file}:{c.line}`"})
                except Exception:
                    log.warning("fallback_note_failed", file=c.file, line=c.line, exc_info=True)
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
                log.warning("inline_comment_failed", file=c.file, line=c.line, exc_info=True)
                try:
                    mr.notes.create({"body": f"{body}\n\n`{c.file}:{c.line}`"})
                except Exception:
                    log.warning(
                        "fallback_note_also_failed",
                        file=c.file,
                        line=c.line,
                        exc_info=True,
                    )

        # Handle resolutions for prior feedback
        if review.resolutions:
            resolved = _handle_resolutions(
                mr, review.resolutions, resolution_behavior, allowed_discussion_ids
            )
            if resolved > 0:
                log.info("discussions_resolved", count=resolved)

        summary_body = f"## Code Review Summary\n\n{review.summary}"
        if head_sha:
            summary_body += f"\n\n{format_sha_marker(head_sha)}"
        mr.notes.create({"body": summary_body})

    await asyncio.to_thread(_post)
