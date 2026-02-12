"""Post review comments to GitLab MR as inline discussions and summary."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import gitlab as gl

    from gitlab_copilot_agent.comment_parser import ParsedReview
    from gitlab_copilot_agent.gitlab_client import MRDiffRef

log = logging.getLogger(__name__)


async def post_review(
    gitlab_client: gl.Gitlab,
    project_id: int,
    mr_iid: int,
    diff_refs: MRDiffRef,
    review: ParsedReview,
) -> None:
    """Post inline comments and summary note to a GitLab MR."""

    def _post() -> None:
        project = gitlab_client.projects.get(project_id)
        mr = project.mergerequests.get(mr_iid)

        for c in review.comments:
            body = f"**[{c.severity.upper()}]** {c.comment}"
            if c.suggestion is not None:
                start = c.suggestion_start_offset
                end = c.suggestion_end_offset
                body += f"\n\n```suggestion:-{start}+{end}\n{c.suggestion}\n```"
            try:
                mr.discussions.create({
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
                })
            except Exception:
                log.warning("Inline comment failed for %s:%d", c.file, c.line, exc_info=True)  # stdlib logger
                mr.notes.create({"body": f"{body}\n\n`{c.file}:{c.line}`"})

        mr.notes.create({"body": f"## Code Review Summary\n\n{review.summary}"})

    await asyncio.to_thread(_post)
