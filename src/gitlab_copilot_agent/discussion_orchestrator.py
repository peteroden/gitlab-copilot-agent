"""Unified discussion handler — thread interactions via @mention or reply.

Handles questions, coding requests, and resolution signals through a single
LLM session with full context (repo, diff, discussion history).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import gitlab
import structlog

from gitlab_copilot_agent.coding_workflow import apply_coding_result
from gitlab_copilot_agent.discussion_engine import (
    build_discussion_prompt,
    parse_discussion_response,
    run_discussion,
)
from gitlab_copilot_agent.discussion_models import DiscussionHistory
from gitlab_copilot_agent.error_messages import branch_deleted_message, user_error_message
from gitlab_copilot_agent.git_operations import (
    TransientCloneError,
    git_commit,
    git_push,
    validate_clone_url_host,
)
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.prompt_defaults import get_prompt
from gitlab_copilot_agent.task_executor import CodingResult, TaskExecutionError
from gitlab_copilot_agent.telemetry import get_tracer

if TYPE_CHECKING:
    from gitlab_copilot_agent.concurrency import DistributedLock
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.discussion_models import AgentIdentity, Discussion
    from gitlab_copilot_agent.models import NoteWebhookPayload
    from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()
_tracer = get_tracer(__name__)

AGENT_AUTHOR_NAME = "Copilot Agent"
AGENT_AUTHOR_EMAIL = "copilot-agent@noreply.gitlab.com"


def _find_triggering_discussion(
    discussions: list[Discussion],
    note_id: int,
) -> Discussion | None:
    """Find the discussion containing the note that triggered this handler."""
    for disc in discussions:
        for note in disc.notes:
            if note.note_id == note_id:
                return disc
    return None


async def handle_discussion_interaction(
    settings: Settings,
    payload: NoteWebhookPayload,
    executor: TaskExecutor,
    agent_identity: AgentIdentity,
    project_token: str | None = None,
    repo_locks: DistributedLock | None = None,
) -> None:
    """Handle an @mention or thread-reply interaction on an MR.

    Full pipeline: clone → fetch context → build prompt → LLM → post reply.
    If the LLM returns code changes, also commit and push.
    """
    mr = payload.merge_request
    project = payload.project
    note_id = payload.object_attributes.id

    with _tracer.start_as_current_span(
        "mr.discussion_interaction",
        attributes={"project_id": project.id, "mr_iid": mr.iid},
    ):
        bound_log = log.bind(project_id=project.id, mr_iid=mr.iid, note_id=note_id)
        await bound_log.ainfo("discussion_interaction_started")

        token = project_token or settings.gitlab_token
        gl_client = GitLabClient(settings.gitlab_url, token)
        repo_path: Path | None = None

        async def _execute() -> None:
            nonlocal repo_path
            try:
                # 1. Clone repo (always — questions may need full context)
                try:
                    validate_clone_url_host(project.git_http_url, settings.gitlab_url)
                    repo_path = await gl_client.clone_repo(
                        project.git_http_url,
                        mr.source_branch,
                        token,
                        clone_dir=settings.clone_dir,
                    )
                except (RuntimeError, TransientCloneError) as clone_exc:
                    clone_err = str(clone_exc).lower()
                    if "not found" in clone_err or "not allowed" in clone_err:
                        await bound_log.awarning(
                            "branch_deleted_or_inaccessible",
                            branch=mr.source_branch,
                            error=str(clone_exc),
                        )
                        # Try to reply in the triggering thread
                        try:
                            discussions = await gl_client.list_mr_discussions(project.id, mr.iid)
                            triggering = _find_triggering_discussion(discussions, note_id)
                            if triggering:
                                gl = gitlab.Gitlab(settings.gitlab_url, private_token=token)
                                gl_project = gl.projects.get(project.id)
                                gl_mr = gl_project.mergerequests.get(mr.iid)
                                disc_obj = gl_mr.discussions.get(triggering.discussion_id)
                                await asyncio.to_thread(
                                    disc_obj.notes.create,
                                    {"body": branch_deleted_message(mr.source_branch)},
                                )
                        except Exception:
                            await bound_log.awarning("branch_deleted_reply_failed", exc_info=True)
                        return
                    raise

                # 2. Fetch MR details + discussions
                mr_details = await gl_client.get_mr_details(project.id, mr.iid)
                discussions = await gl_client.list_mr_discussions(project.id, mr.iid)
                discussion_history = DiscussionHistory(
                    discussions=discussions, agent=agent_identity
                )

                # 3. Find the triggering discussion thread
                triggering = _find_triggering_discussion(discussions, note_id)
                if triggering is None:
                    await bound_log.awarning("triggering_discussion_not_found", note_id=note_id)
                    return

                # 4. Build prompt + run LLM
                user_prompt = build_discussion_prompt(mr_details, discussion_history, triggering)
                result = await run_discussion(
                    executor,
                    settings,
                    str(repo_path),
                    project.git_http_url,
                    system_prompt=get_prompt(settings, "discussion"),
                    user_prompt=user_prompt,
                    source_branch=mr.source_branch,
                    note_id=note_id,
                )

                # 5. Parse response — use summary as reply text
                response = parse_discussion_response(result.summary)

                # 6. If the task runner captured code changes, apply and push
                has_patch = isinstance(result, CodingResult) and bool(result.patch)
                await bound_log.ainfo(
                    "discussion_response_parsed",
                    has_code_changes=has_patch,
                )

                if has_patch:
                    await apply_coding_result(result, repo_path)
                    has_changes = await git_commit(
                        repo_path,
                        f"fix: {payload.object_attributes.note[:50]}",
                        AGENT_AUTHOR_NAME,
                        AGENT_AUTHOR_EMAIL,
                    )
                    if has_changes:
                        await git_push(repo_path, "origin", mr.source_branch, token)
                        response = response.model_copy(
                            update={"reply": f"{response.reply}\n\n✅ Changes pushed."}
                        )

                # 7. Post reply to the existing thread
                gl = gitlab.Gitlab(settings.gitlab_url, private_token=token)
                gl_project = gl.projects.get(project.id)
                gl_mr = gl_project.mergerequests.get(mr.iid)
                disc_obj = gl_mr.discussions.get(triggering.discussion_id)
                await asyncio.to_thread(disc_obj.notes.create, {"body": response.reply})
                await bound_log.ainfo("discussion_reply_posted")

            except TaskExecutionError as exc:
                error_str = str(exc)
                await bound_log.aerror(
                    "discussion_task_failed",
                    error=error_str,
                )
                try:
                    await gl_client.post_mr_comment(
                        project.id, mr.iid, user_error_message(error_str)
                    )
                except Exception:
                    await bound_log.awarning("error_comment_failed", exc_info=True)
                raise
            except Exception as exc:
                await bound_log.aerror(
                    "discussion_interaction_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                try:
                    await gl_client.post_mr_comment(
                        project.id,
                        mr.iid,
                        "❌ Unable to process your request. "
                        "The service encountered an unexpected error. "
                        "Please try again or contact the project administrator.",
                    )
                except Exception:
                    await bound_log.awarning("error_comment_failed", exc_info=True)
                raise
            finally:
                if repo_path:
                    await asyncio.to_thread(shutil.rmtree, repo_path, True)

        if repo_locks:
            async with repo_locks.acquire(project.git_http_url):
                await _execute()
        else:
            await _execute()
