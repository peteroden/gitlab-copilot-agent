"""Post review comments to GitLab MR as inline discussions and summary."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.incremental import format_sha_marker

if TYPE_CHECKING:
    from gitlab_copilot_agent.comment_parser import ParsedReview, Resolution
    from gitlab_copilot_agent.gitlab_client import GitLabClient, MRChange, MRDiffRef
    from gitlab_copilot_agent.mapping_models import ResolutionBehavior

log = structlog.get_logger()


def _build_activity_section(
    posted_inline: int,
    posted_fallback: int,
    resolutions: list[Resolution],
    resolved_count: int,
) -> str:
    """Compose a markdown activity summary from posting outcomes and resolution data.

    Returns empty string when all counts are zero (clean review with no prior threads).
    """
    lines: list[str] = []
    total_posted = posted_inline + posted_fallback
    if total_posted:
        lines.append(f"- **{total_posted}** new comment{'s' if total_posted != 1 else ''}")
    if resolved_count:
        lines.append(f"- **{resolved_count}** thread{'s' if resolved_count != 1 else ''} resolved")
    partial_count = sum(1 for r in resolutions if r.status == "partial")
    if partial_count:
        lines.append(
            f"- **{partial_count}** partial resolution{'s' if partial_count != 1 else ''}"
        )
    if not lines:
        return ""
    return "### 📊 Review Activity\n\n" + "\n".join(lines)


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


async def _handle_resolutions(
    gl_client: GitLabClient,
    project_id: int,
    mr_iid: int,
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
            if resolution_behavior == "auto-resolve" and r.status == "resolved":
                await gl_client.reply_to_discussion(
                    project_id, mr_iid, r.discussion_id, f"✅ {r.message}"
                )
                await gl_client.resolve_discussion(project_id, mr_iid, r.discussion_id)
                resolved_count += 1
            elif r.status in ("resolved", "partial"):
                prefix = "✅" if r.status == "resolved" else "⚠️"
                await gl_client.reply_to_discussion(
                    project_id, mr_iid, r.discussion_id, f"{prefix} {r.message}"
                )
        except Exception:
            log.warning(
                "resolution_action_failed",
                discussion_id=r.discussion_id,
                exc_info=True,
            )

    return resolved_count


async def post_review(
    gl_client: GitLabClient,
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
    # Precompute valid positions once for all comments
    valid_positions: set[tuple[str, int]] = set()
    diff_files: set[str] = set()
    for change in changes:
        diff_files.add(change.new_path)
        valid_positions |= _parse_hunk_lines(change.diff, change.new_path)

    # Track posting outcomes for activity summary
    posted_inline = 0
    posted_fallback = 0
    skipped = 0

    for c in review.comments:
        body = f"**[{c.severity.upper()}]** {c.comment}"
        if c.suggestion is not None:
            start = c.suggestion_start_offset
            end = c.suggestion_end_offset
            body += f"\n\n```suggestion:-{start}+{end}\n{c.suggestion}\n```"

        # Skip comments on files not in the diff entirely
        if c.file not in diff_files:
            log.info("comment_skipped_not_in_diff", file=c.file, line=c.line)
            skipped += 1
            continue

        # Validate position before attempting inline comment
        if not _is_valid_position(c.file, c.line, valid_positions):
            log.warning(
                "comment_position_invalid",
                file=c.file,
                line=c.line,
            )
            try:
                await gl_client.post_mr_comment(
                    project_id, mr_iid, f"{body}\n\n`{c.file}:{c.line}`"
                )
                posted_fallback += 1
            except Exception:
                log.warning("fallback_note_failed", file=c.file, line=c.line, exc_info=True)
                skipped += 1
            continue

        # Position is valid, attempt inline comment
        try:
            await gl_client.create_mr_discussion(
                project_id,
                mr_iid,
                body,
                {
                    "base_sha": diff_refs.base_sha,
                    "start_sha": diff_refs.start_sha,
                    "head_sha": diff_refs.head_sha,
                    "position_type": "text",
                    "old_path": c.file,
                    "new_path": c.file,
                    "new_line": c.line,
                },
            )
            posted_inline += 1
        except Exception:
            log.warning("inline_comment_failed", file=c.file, line=c.line, exc_info=True)
            try:
                await gl_client.post_mr_comment(
                    project_id, mr_iid, f"{body}\n\n`{c.file}:{c.line}`"
                )
                posted_fallback += 1
            except Exception:
                log.warning(
                    "fallback_note_also_failed",
                    file=c.file,
                    line=c.line,
                    exc_info=True,
                )
                skipped += 1

    # Handle resolutions for prior feedback
    resolved = 0
    if review.resolutions:
        resolved = await _handle_resolutions(
            gl_client,
            project_id,
            mr_iid,
            review.resolutions,
            resolution_behavior,
            allowed_discussion_ids,
        )
        if resolved > 0:
            log.info("discussions_resolved", count=resolved)

    # Compose summary note with optional activity section
    summary_body = f"## Code Review Summary\n\n{review.summary}"
    activity = _build_activity_section(
        posted_inline, posted_fallback, review.resolutions or [], resolved
    )
    if activity:
        summary_body += f"\n\n{activity}"
    if head_sha:
        summary_body += f"\n\n{format_sha_marker(head_sha)}"
    await gl_client.post_mr_comment(project_id, mr_iid, summary_body)
