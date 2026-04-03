"""Orchestrator — wires webhook → clone → review → post.

Fetches MR discussion history and agent identity for context-aware reviews.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import gitlab
import structlog

from gitlab_copilot_agent.comment_parser import parse_review
from gitlab_copilot_agent.comment_poster import post_review
from gitlab_copilot_agent.discussion_models import DiscussionHistory
from gitlab_copilot_agent.error_messages import user_error_message
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.metrics import reviews_duration, reviews_total
from gitlab_copilot_agent.review_engine import ReviewRequest, run_review
from gitlab_copilot_agent.task_executor import TaskExecutionError
from gitlab_copilot_agent.telemetry import get_tracer

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.credential_registry import CredentialRegistry
    from gitlab_copilot_agent.models import MergeRequestWebhookPayload
    from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()
_tracer = get_tracer(__name__)


async def handle_review(
    settings: Settings,
    payload: MergeRequestWebhookPayload,
    executor: TaskExecutor,
    project_token: str | None = None,
    credential_registry: CredentialRegistry | None = None,
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

        bound_log.info("review_started")

        token = project_token or settings.gitlab_token
        gl_client = GitLabClient(settings.gitlab_url, token)
        repo_path: Path | None = None

        try:
            repo_path = await gl_client.clone_repo(
                project.git_http_url,
                mr.source_branch,
                token,
                clone_dir=settings.clone_dir,
            )

            mr_details = await gl_client.get_mr_details(project.id, mr.iid)

            # Fetch discussion history for context (requires credential_registry
            # to resolve agent identity — skip entirely without it)
            discussion_history: DiscussionHistory | None = None
            if credential_registry is not None:
                try:
                    discussions = await gl_client.list_mr_discussions(project.id, mr.iid)
                    agent_identity = await credential_registry.resolve_identity(
                        "default", settings.gitlab_url
                    )
                    discussion_history = DiscussionHistory(
                        discussions=discussions, agent=agent_identity
                    )
                    bound_log.info(
                        "discussion_history_loaded",
                        discussion_count=len(discussions),
                        agent_user_id=agent_identity.user_id,
                    )
                except Exception:
                    bound_log.warning("discussion_history_failed", exc_info=True)
            else:
                bound_log.debug("discussion_history_skipped", reason="no_credential_registry")

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
                discussion_history=discussion_history,
            )
            parsed = parse_review(raw_result.summary)

            bound_log.info(
                "review_complete",
                inline_comments=len(parsed.comments),
            )

            gl = gitlab.Gitlab(settings.gitlab_url, private_token=token)

            await post_review(
                gl, project.id, mr.iid, mr_details.diff_refs, parsed, mr_details.changes
            )
            bound_log.info("comments_posted")
            outcome = "success"
        except TaskExecutionError as exc:
            error_str = str(exc)
            bound_log.error("review_task_failed", error=error_str)
            try:
                await gl_client.post_mr_comment(
                    project.id,
                    mr.iid,
                    f"⚠️ Automated review failed.\n\n{user_error_message(error_str)}",
                )
            except Exception:
                bound_log.warning("failure_comment_post_failed", exc_info=True)
            raise
        except Exception as exc:
            bound_log.error(
                "review_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            try:
                await gl_client.post_mr_comment(
                    project.id,
                    mr.iid,
                    "⚠️ Automated review failed. "
                    "The service encountered an unexpected error. "
                    "Please try again or contact the project administrator.",
                )
            except Exception:
                bound_log.warning("failure_comment_post_failed", exc_info=True)
            raise
        finally:
            if repo_path:
                await gl_client.cleanup(repo_path)
            reviews_total.add(1, {"outcome": outcome})
            reviews_duration.record(time.monotonic() - start, {"outcome": outcome})
