"""Unified discussion handler — thread interactions via @mention or reply.

Handles questions, coding requests, and resolution signals through a single
LLM session with full context (repo, diff, discussion history).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import gitlab
import structlog

from gitlab_copilot_agent.coding_workflow import apply_coding_result
from gitlab_copilot_agent.concurrency import DistributedLock
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.discussion_engine import (
    build_discussion_prompt,
    parse_discussion_response,
    run_discussion,
)
from gitlab_copilot_agent.discussion_models import AgentIdentity, Discussion, DiscussionHistory
from gitlab_copilot_agent.error_messages import branch_deleted_message, user_error_message
from gitlab_copilot_agent.events import TaskEvent
from gitlab_copilot_agent.git_operations import (
    TransientCloneError,
    git_commit,
    git_push,
    validate_clone_url_host,
)
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.mapping_models import ResolutionBehavior
from gitlab_copilot_agent.prompt_defaults import get_prompt
from gitlab_copilot_agent.task_executor import (
    CodingResult,
    TaskExecutionError,
    TaskExecutor,
)
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)


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
    event: TaskEvent,
    executor: TaskExecutor,
    agent_identity: AgentIdentity,
    repo_locks: DistributedLock | None = None,
) -> None:
    """Handle an @mention or thread-reply interaction on an MR.

    Full pipeline: clone → fetch context → build prompt → LLM → post reply.
    If the LLM returns code changes, also commit and push.
    """
    project_id = event.project_id
    mr_iid = event.mr_iid
    if mr_iid is None:
        msg = "discussion events require mr_iid"
        raise ValueError(msg)
    note_id = event.note_id
    if note_id is None:
        msg = "discussion events require note_id"
        raise ValueError(msg)
    clone_url = event.clone_url
    token = event.token
    resolution_behavior: ResolutionBehavior = event.resolution_behavior

    with _tracer.start_as_current_span(
        "mr.discussion_interaction",
        attributes={"project_id": project_id, "mr_iid": mr_iid},
    ):
        bound_log = log.bind(project_id=project_id, mr_iid=mr_iid, note_id=note_id)
        await bound_log.ainfo("discussion_interaction_started")

        gl_client = GitLabClient(settings.gitlab_url, token)
        repo_path: Path | None = None

        async def _execute() -> None:
            nonlocal repo_path
            try:
                # 1. Clone repo (always — questions may need full context)
                try:
                    validate_clone_url_host(clone_url, settings.gitlab_url)
                    repo_path = await gl_client.clone_repo(
                        clone_url,
                        event.branch,
                        token,
                        clone_dir=settings.clone_dir,
                    )
                except (RuntimeError, TransientCloneError) as clone_exc:
                    clone_err = str(clone_exc).lower()
                    if "not found" in clone_err or "not allowed" in clone_err:
                        await bound_log.awarning(
                            "branch_deleted_or_inaccessible",
                            branch=event.branch,
                            error=str(clone_exc),
                        )
                        # Try to reply in the triggering thread
                        try:
                            discussions = await gl_client.list_mr_discussions(project_id, mr_iid)
                            triggering = _find_triggering_discussion(discussions, note_id)
                            if triggering:
                                gl = gitlab.Gitlab(settings.gitlab_url, private_token=token)
                                gl_project = gl.projects.get(project_id)
                                gl_mr = gl_project.mergerequests.get(mr_iid)
                                disc_obj = gl_mr.discussions.get(triggering.discussion_id)
                                await asyncio.to_thread(
                                    disc_obj.notes.create,
                                    {"body": branch_deleted_message(event.branch)},
                                )
                        except Exception:
                            await bound_log.awarning("branch_deleted_reply_failed", exc_info=True)
                        return
                    raise

                # 2. Fetch MR details + discussions
                mr_details = await gl_client.get_mr_details(project_id, mr_iid)
                discussions = await gl_client.list_mr_discussions(project_id, mr_iid)
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
                    clone_url,
                    system_prompt=get_prompt(settings, "discussion"),
                    user_prompt=user_prompt,
                    source_branch=event.branch,
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

                # Build commit message from note body or fallback
                commit_subject = (event.note_body or "discussion fix")[:50]

                if has_patch:
                    await apply_coding_result(result, repo_path)
                    has_changes = await git_commit(
                        repo_path,
                        f"fix: {commit_subject}",
                        settings.agent_author_name,
                        settings.agent_author_email,
                    )
                    if has_changes:
                        await git_push(repo_path, "origin", event.branch, token)
                        response = response.model_copy(
                            update={"reply": f"{response.reply}\n\n✅ Changes pushed."}
                        )

                # 7. Post reply to the existing thread
                gl = gitlab.Gitlab(settings.gitlab_url, private_token=token)
                gl_project = gl.projects.get(project_id)
                gl_mr = gl_project.mergerequests.get(mr_iid)
                disc_obj = gl_mr.discussions.get(triggering.discussion_id)
                await asyncio.to_thread(disc_obj.notes.create, {"body": response.reply})
                await bound_log.ainfo("discussion_reply_posted")

                # 8. Handle resolution if the LLM determined one
                # Only auto-resolve threads originally created by the agent
                first_note = triggering.notes[0] if triggering.notes else None
                is_agent_thread = (
                    first_note is not None
                    and triggering.is_inline
                    and first_note.author_id == agent_identity.user_id
                )
                if response.resolution and resolution_behavior != "off" and is_agent_thread:
                    try:
                        if (
                            response.resolution.status == "resolved"
                            and resolution_behavior == "auto-resolve"
                        ):
                            disc_obj.resolved = True
                            await asyncio.to_thread(disc_obj.save)
                            await bound_log.ainfo(
                                "discussion_auto_resolved",
                                discussion_id=triggering.discussion_id,
                            )
                        elif response.resolution.status == "partial":
                            await bound_log.ainfo(
                                "discussion_partial_resolution",
                                discussion_id=triggering.discussion_id,
                            )
                    except Exception:
                        await bound_log.awarning(
                            "discussion_resolution_failed",
                            discussion_id=triggering.discussion_id,
                            exc_info=True,
                        )

            except TaskExecutionError as exc:
                error_str = str(exc)
                await bound_log.aerror(
                    "discussion_task_failed",
                    error=error_str,
                )
                try:
                    await gl_client.post_mr_comment(
                        project_id, mr_iid, user_error_message(error_str)
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
                        project_id,
                        mr_iid,
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
            async with repo_locks.acquire(clone_url):
                await _execute()
        else:
            await _execute()
