"""Handle /copilot commands on GitLab MR comments."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import structlog

from gitlab_copilot_agent.approval_store import ApprovalStore
from gitlab_copilot_agent.coding_engine import CODING_SYSTEM_PROMPT
from gitlab_copilot_agent.coding_workflow import apply_coding_result
from gitlab_copilot_agent.concurrency import DistributedLock
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.git_operations import git_clone, git_commit, git_push
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.models import NoteWebhookPayload, PendingApproval
from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)

COPILOT_PREFIX = "/copilot "
COPILOT_APPROVE = "/copilot approve"
AGENT_AUTHOR_NAME = "Copilot Agent"
AGENT_AUTHOR_EMAIL = "copilot-agent@noreply.gitlab.com"


def parse_copilot_command(note: str) -> str | None:
    """Extract instruction from a /copilot command. Returns None if not a command."""
    stripped = note.strip()
    if stripped.lower() == COPILOT_APPROVE.lower():
        return None  # Approval commands are handled separately
    if stripped.lower().startswith(COPILOT_PREFIX):
        return stripped[len(COPILOT_PREFIX) :].strip() or None
    return None


def is_approval_command(note: str) -> bool:
    """Check if note is a /copilot approve command."""
    return note.strip().lower() == COPILOT_APPROVE.lower()


def build_mr_coding_prompt(
    instruction: str, mr_title: str, source_branch: str, target_branch: str
) -> str:
    """Build user prompt for an MR comment coding task."""
    return (
        f"## MR: {mr_title}\n"
        f"**Branch:** {source_branch} → {target_branch}\n"
        f"**Instruction:** {instruction}\n\n"
        f"Implement the requested changes on this merge request. "
        f"Explore the repository, make the changes, run tests, "
        f"and provide a summary of what you did."
    )


async def handle_copilot_comment(
    settings: Settings,
    payload: NoteWebhookPayload,
    executor: TaskExecutor,
    repo_locks: DistributedLock | None = None,
    approval_store: ApprovalStore | None = None,
) -> None:
    """Handle a /copilot command from an MR comment."""
    mr = payload.merge_request
    project = payload.project
    note = payload.object_attributes.note

    # Handle approval command
    if is_approval_command(note):
        if not approval_store:
            return
        await _handle_approval(settings, payload, executor, repo_locks, approval_store)
        return

    # Handle regular command
    instruction = parse_copilot_command(note)
    if not instruction:
        return

    with _tracer.start_as_current_span(
        "mr.copilot_command", attributes={"project_id": project.id, "mr_iid": mr.iid}
    ):
        bound_log = log.bind(project_id=project.id, mr_iid=mr.iid)
        await bound_log.ainfo("copilot_command_received", instruction=instruction[:100])

        gl_client = GitLabClient(settings.gitlab_url, settings.gitlab_token)

        # If approval required, store and wait (fail-closed: refuse if store missing)
        if settings.copilot_require_approval:
            if not approval_store:
                await bound_log.awarning("copilot_approval_required_but_no_store")
                return
            task_id = f"mr-{project.id}-{mr.iid}"
            approval = PendingApproval(
                task_id=task_id,
                requester_id=payload.user.id,
                prompt=instruction,
                mr_iid=mr.iid,
                project_id=project.id,
                timeout=settings.copilot_approval_timeout,
            )
            await approval_store.store(approval)
            await gl_client.post_mr_comment(
                project.id,
                mr.iid,
                "⏳ Approval required. React with 👍 or reply `/copilot approve` to proceed.",
            )
            await bound_log.ainfo("copilot_command_pending_approval")
            return

        # Execute immediately if no approval required
        await _execute_copilot_task(
            settings, payload, instruction, executor, repo_locks, bound_log
        )


async def _handle_approval(
    settings: Settings,
    payload: NoteWebhookPayload,
    executor: TaskExecutor,
    repo_locks: DistributedLock | None,
    approval_store: ApprovalStore,
) -> None:
    """Handle /copilot approve command."""
    mr = payload.merge_request
    project = payload.project
    bound_log = log.bind(project_id=project.id, mr_iid=mr.iid)

    # Look up pending approval
    pending = await approval_store.get(project.id, mr.iid)
    if not pending:
        await bound_log.ainfo("approval_command_no_pending")
        return

    # Verify requester matches
    if pending.requester_id != payload.user.id:
        await bound_log.ainfo(
            "approval_command_wrong_user",
            requester_id=pending.requester_id,
            approver_id=payload.user.id,
        )
        gl_client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        await gl_client.post_mr_comment(
            project.id, mr.iid, "❌ Only the original requester can approve this command."
        )
        return

    # Delete pending approval and execute
    await approval_store.delete(project.id, mr.iid)
    await bound_log.ainfo("copilot_command_approved", prompt=pending.prompt[:100])
    await _execute_copilot_task(settings, payload, pending.prompt, executor, repo_locks, bound_log)


async def _execute_copilot_task(
    settings: Settings,
    payload: NoteWebhookPayload,
    instruction: str,
    executor: TaskExecutor,
    repo_locks: DistributedLock | None,
    bound_log,
) -> None:
    """Execute the copilot task with the given instruction."""
    mr = payload.merge_request
    project = payload.project
    gl_client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    repo_path: Path | None = None

    async def _execute() -> None:
        nonlocal repo_path
        try:
            repo_path = await git_clone(
                project.git_http_url,
                mr.source_branch,
                settings.gitlab_token,
                clone_dir=settings.clone_dir,
            )
            task = TaskParams(
                task_type="coding",
                task_id=f"mr-{project.id}-{mr.iid}",
                repo_url=project.git_http_url,
                branch=mr.source_branch,
                system_prompt=CODING_SYSTEM_PROMPT,
                user_prompt=build_mr_coding_prompt(
                    instruction, mr.title, mr.source_branch, mr.target_branch
                ),
                settings=settings,
                repo_path=str(repo_path),
            )
            result = await executor.execute(task)
            await bound_log.ainfo("copilot_coding_complete", summary=result.summary[:200])

            await apply_coding_result(result, repo_path)
            has_changes = await git_commit(
                repo_path, f"fix: {instruction[:50]}", AGENT_AUTHOR_NAME, AGENT_AUTHOR_EMAIL
            )
            if has_changes:
                await git_push(repo_path, "origin", mr.source_branch, settings.gitlab_token)
                await gl_client.post_mr_comment(
                    project.id, mr.iid, f"✅ Changes pushed.\n\n{result.summary}"
                )
            else:
                await gl_client.post_mr_comment(
                    project.id, mr.iid, f"ℹ️ No file changes needed.\n\n{result.summary}"
                )

            await bound_log.ainfo("copilot_command_complete")
        except Exception:
            await bound_log.aexception("copilot_command_failed")
            try:
                await gl_client.post_mr_comment(
                    project.id,
                    mr.iid,
                    "❌ Agent encountered an error processing your request.",
                )
            except Exception:
                await bound_log.aexception("error_comment_failed")
            raise
        finally:
            if repo_path:
                await asyncio.to_thread(shutil.rmtree, repo_path, True)

    if repo_locks:
        async with repo_locks.acquire(project.git_http_url):
            await _execute()
    else:
        await _execute()
