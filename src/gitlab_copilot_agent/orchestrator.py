"""Orchestrator — wires webhook → clone → review → post."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gitlab
import structlog

from gitlab_copilot_agent.comment_parser import parse_review
from gitlab_copilot_agent.comment_poster import post_review
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.review_engine import ReviewRequest, run_review

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.models import MergeRequestWebhookPayload

log = structlog.get_logger()


async def handle_review(settings: Settings, payload: MergeRequestWebhookPayload) -> None:
    """Full review pipeline: clone → review → parse → post comments."""
    mr = payload.object_attributes
    project = payload.project
    bound_log = log.bind(project_id=project.id, mr_iid=mr.iid)

    await bound_log.ainfo("review_started")

    gl_client = GitLabClient(settings.gitlab_url, settings.gitlab_token)

    repo_path = await gl_client.clone_repo(
        project.git_http_url, mr.source_branch, settings.gitlab_token
    )

    try:
        review_req = ReviewRequest(
            title=mr.title,
            description=mr.description,
            source_branch=mr.source_branch,
            target_branch=mr.target_branch,
        )

        raw_review = await run_review(settings, str(repo_path), review_req)
        parsed = parse_review(raw_review)

        await bound_log.ainfo(
            "review_complete",
            inline_comments=len(parsed.comments),
        )

        mr_details = await gl_client.get_mr_details(project.id, mr.iid)
        gl = gitlab.Gitlab(settings.gitlab_url, private_token=settings.gitlab_token)

        await post_review(gl, project.id, mr.iid, mr_details.diff_refs, parsed)
        await bound_log.ainfo("comments_posted")
    except Exception:
        await bound_log.aexception("review_failed")
        raise
    finally:
        await gl_client.cleanup(repo_path)
