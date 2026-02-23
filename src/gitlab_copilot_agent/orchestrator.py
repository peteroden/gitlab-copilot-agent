"""Orchestrator — wires webhook → clone → review → post."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import gitlab
import structlog

from gitlab_copilot_agent.comment_parser import parse_review
from gitlab_copilot_agent.comment_poster import post_review
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.metrics import reviews_duration, reviews_total
from gitlab_copilot_agent.review_engine import ReviewRequest, run_review
from gitlab_copilot_agent.telemetry import get_tracer

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.models import MergeRequestWebhookPayload
    from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()
_tracer = get_tracer(__name__)


async def handle_review(
    settings: Settings, payload: MergeRequestWebhookPayload, executor: TaskExecutor
) -> None:
    """Full review pipeline: clone → review → parse → post comments."""
    mr = payload.object_attributes
    project = payload.project
    start = time.monotonic()
    outcome = "error"
    with _tracer.start_as_current_span(
        "mr.review", attributes={"project_id": project.id, "mr_iid": mr.iid}
    ):
        bound_log = log.bind(project_id=project.id, mr_iid=mr.iid)

        await bound_log.ainfo("review_started")

        gl_client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        repo_path: Path | None = None

        try:
            repo_path = await gl_client.clone_repo(
                project.git_http_url,
                mr.source_branch,
                settings.gitlab_token,
                clone_dir=settings.clone_dir,
            )

            mr_details = await gl_client.get_mr_details(project.id, mr.iid)

            # Build diff text from MR changes so the LLM reviews only the diff
            diff_text = "\n".join(
                f"--- a/{c.old_path}\n+++ b/{c.new_path}\n{c.diff}" for c in mr_details.changes
            )

            review_req = ReviewRequest(
                title=mr.title,
                description=mr.description,
                source_branch=mr.source_branch,
                target_branch=mr.target_branch,
            )

            raw_result = await run_review(
                executor,
                settings,
                str(repo_path),
                project.git_http_url,
                review_req,
                diff_text=diff_text,
            )
            parsed = parse_review(raw_result.summary)

            await bound_log.ainfo(
                "review_complete",
                inline_comments=len(parsed.comments),
            )

            gl = gitlab.Gitlab(settings.gitlab_url, private_token=settings.gitlab_token)

            await post_review(
                gl, project.id, mr.iid, mr_details.diff_refs, parsed, mr_details.changes
            )
            await bound_log.ainfo("comments_posted")
            outcome = "success"
        except Exception:
            await bound_log.aexception("review_failed")
            try:
                await gl_client.post_mr_comment(
                    project.id,
                    mr.iid,
                    "⚠️ Automated review failed. Check service logs for details.",
                )
            except Exception:
                await bound_log.aexception("failure_comment_post_failed")
            raise
        finally:
            if repo_path:
                await gl_client.cleanup(repo_path)
            reviews_total.add(1, {"outcome": outcome})
            reviews_duration.record(time.monotonic() - start, {"outcome": outcome})
