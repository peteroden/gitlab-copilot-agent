"""Background GitLab poller — discovers new/updated MRs and /copilot notes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import structlog

from gitlab_copilot_agent.concurrency import DeduplicationStore, DistributedLock
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.gitlab_client import (
    GitLabClientProtocol,
    MRListItem,
    NoteListItem,
)
from gitlab_copilot_agent.models import (
    MergeRequestWebhookPayload,
    MRLastCommit,
    MRObjectAttributes,
    NoteMergeRequest,
    NoteObjectAttributes,
    NoteWebhookPayload,
    WebhookProject,
    WebhookUser,
)
from gitlab_copilot_agent.mr_comment_handler import handle_copilot_comment, parse_copilot_command
from gitlab_copilot_agent.orchestrator import handle_review
from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()
_DEDUP_TTL = 86400
_MAX_BACKOFF = 300


class GitLabPoller:
    """Background poller that discovers open MRs and /copilot notes."""

    def __init__(
        self,
        gl_client: GitLabClientProtocol,
        settings: Settings,
        project_ids: set[int],
        dedup: DeduplicationStore,
        executor: TaskExecutor,
        repo_locks: DistributedLock | None = None,
    ) -> None:
        self._client = gl_client
        self._settings = settings
        self._project_ids = project_ids
        self._dedup = dedup
        self._executor = executor
        self._repo_locks = repo_locks
        self._interval: int = 30
        self._task: asyncio.Task[None] | None = None
        self._watermark: str | None = None
        self._note_watermark: str | None = None
        self._failures: int = 0

    async def start(self) -> None:
        # MR watermark looks back to catch recently created/updated MRs
        if self._watermark is None:
            lookback = self._settings.gitlab_poll_lookback
            self._watermark = (datetime.now(UTC) - timedelta(minutes=lookback)).isoformat()
        # Note watermark starts at "now" to avoid replaying /copilot commands
        if self._note_watermark is None:
            self._note_watermark = datetime.now(UTC).isoformat()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
                self._failures = 0
            except Exception:
                self._failures += 1
                await log.aexception("gitlab_poll_error")
            await asyncio.sleep(min(self._interval * 2**self._failures, _MAX_BACKOFF))

    async def _poll_once(self) -> None:
        poll_start = datetime.now(UTC).isoformat()
        for pid in self._project_ids:
            mrs = await self._client.list_project_mrs(
                pid, state="opened", updated_after=self._watermark
            )
            for mr in mrs:
                await self._process_mr(pid, mr)
            await self._process_notes(pid, mrs)
        self._watermark = poll_start
        self._note_watermark = poll_start

    async def _process_mr(self, project_id: int, mr: MRListItem) -> None:
        if self._settings.gitlab_review_on_push:
            key = f"review:{project_id}:{mr.iid}:{mr.sha}"
        else:
            key = f"review:{project_id}:{mr.iid}"
        if await self._dedup.is_seen(key):
            return
        # Extract path from web_url: https://gitlab.example.com/group/project/-/merge_requests/1
        project_url = mr.web_url.split("/-/")[0]
        ns = project_url.removeprefix(self._settings.gitlab_url).strip("/")
        payload = MergeRequestWebhookPayload(
            object_kind="merge_request",
            user=WebhookUser(id=mr.author.id, username=mr.author.username),
            project=WebhookProject(
                id=project_id,
                path_with_namespace=ns,
                git_http_url=f"{project_url}.git",
            ),
            object_attributes=MRObjectAttributes(
                iid=mr.iid,
                title=mr.title,
                description=mr.description or "",
                action="update",
                source_branch=mr.source_branch,
                target_branch=mr.target_branch,
                last_commit=MRLastCommit(id=mr.sha, message=""),
                url=mr.web_url,
                oldrev=None,
            ),
        )
        await handle_review(self._settings, payload, self._executor)
        await self._dedup.mark_seen(key, ttl_seconds=_DEDUP_TTL)

    async def _process_notes(self, project_id: int, mrs: list[MRListItem]) -> None:
        for mr in mrs:
            notes = await self._client.list_mr_notes(
                project_id, mr.iid, created_after=self._note_watermark
            )
            for note in notes:
                if note.system:
                    continue
                if not parse_copilot_command(note.body):
                    continue
                # Skip self-authored notes (consistent with webhook)
                agent_user = self._settings.agent_gitlab_username
                if agent_user and note.author.username == agent_user:
                    continue
                note_key = f"note:{project_id}:{mr.iid}:{note.id}"
                if await self._dedup.is_seen(note_key):
                    continue
                payload = _build_note_payload(note, mr, project_id, self._settings)
                await handle_copilot_comment(
                    self._settings, payload, self._executor, self._repo_locks
                )
                await self._dedup.mark_seen(note_key, ttl_seconds=_DEDUP_TTL)


def _build_note_payload(
    note: NoteListItem,
    mr: MRListItem,
    project_id: int,
    settings: Settings,
) -> NoteWebhookPayload:
    """Synthesize a NoteWebhookPayload from API models."""
    project_url = mr.web_url.split("/-/")[0]
    ns = project_url.removeprefix(settings.gitlab_url).strip("/")
    return NoteWebhookPayload(
        object_kind="note",
        user=WebhookUser(id=note.author.id, username=note.author.username),
        project=WebhookProject(
            id=project_id,
            path_with_namespace=ns,
            git_http_url=f"{project_url}.git",
        ),
        object_attributes=NoteObjectAttributes(
            note=note.body,
            noteable_type="MergeRequest",
        ),
        merge_request=NoteMergeRequest(
            iid=mr.iid,
            title=mr.title,
            source_branch=mr.source_branch,
            target_branch=mr.target_branch,
        ),
    )
