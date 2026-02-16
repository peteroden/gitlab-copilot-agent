"""Handle /copilot commands on GitLab MR comments."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import structlog

from gitlab_copilot_agent.coding_engine import CODING_SYSTEM_PROMPT
from gitlab_copilot_agent.concurrency import RepoLockManager
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.copilot_session import run_copilot_session
from gitlab_copilot_agent.git_operations import git_clone, git_commit, git_push
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.models import NoteWebhookPayload
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)

COPILOT_PREFIX = "/copilot "
AGENT_AUTHOR_NAME = "Copilot Agent"
AGENT_AUTHOR_EMAIL = "copilot-agent@noreply.gitlab.com"


def parse_copilot_command(note: str) -> str | None:
    """Extract instruction from a /copilot command. Returns None if not a command."""
    stripped = note.strip()
    if stripped.lower().startswith(COPILOT_PREFIX):
        return stripped[len(COPILOT_PREFIX) :].strip() or None
    return None


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
    repo_locks: RepoLockManager | None = None,
) -> None:
    """Handle a /copilot command from an MR comment."""
    mr = payload.merge_request
    project = payload.project
    instruction = parse_copilot_command(payload.object_attributes.note)
    if not instruction:
        return

    with _tracer.start_as_current_span(
        "mr.copilot_command", attributes={"project_id": project.id, "mr_iid": mr.iid}
    ):
        bound_log = log.bind(project_id=project.id, mr_iid=mr.iid)
        await bound_log.ainfo("copilot_command_received", instruction=instruction[:100])

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
                result = await run_copilot_session(
                    settings=settings,
                    repo_path=str(repo_path),
                    system_prompt=CODING_SYSTEM_PROMPT,
                    user_prompt=build_mr_coding_prompt(
                        instruction, mr.title, mr.source_branch, mr.target_branch
                    ),
                )
                await bound_log.ainfo("copilot_coding_complete", summary=result[:200])

                has_changes = await git_commit(
                    repo_path, f"fix: {instruction[:50]}", AGENT_AUTHOR_NAME, AGENT_AUTHOR_EMAIL
                )
                if has_changes:
                    await git_push(repo_path, "origin", mr.source_branch, settings.gitlab_token)
                    await gl_client.post_mr_comment(
                        project.id, mr.iid, f"✅ Changes pushed.\n\n{result}"
                    )
                else:
                    await gl_client.post_mr_comment(
                        project.id, mr.iid, f"ℹ️ No file changes needed.\n\n{result}"
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
