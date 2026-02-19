"""Background GitLab poller — discovers new/updated MRs and triggers reviews."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

import structlog

from gitlab_copilot_agent.concurrency import DeduplicationStore
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.gitlab_client import GitLabClientProtocol, MRListItem
from gitlab_copilot_agent.models import (
    MergeRequestWebhookPayload,
    MRLastCommit,
    MRObjectAttributes,
    WebhookProject,
    WebhookUser,
)
from gitlab_copilot_agent.orchestrator import handle_review
from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()
_DEDUP_TTL = 86400
_MAX_BACKOFF = 300


class GitLabPoller:
    """Background poller that discovers open MRs and triggers reviews."""

    def __init__(
        self,
        gl_client: GitLabClientProtocol,
        settings: Settings,
        project_ids: set[int],
        dedup: DeduplicationStore,
        executor: TaskExecutor,
    ) -> None:
        self._client = gl_client
        self._settings = settings
        self._project_ids = project_ids
        self._dedup = dedup
        self._executor = executor
        self._interval: int = 30
        self._task: asyncio.Task[None] | None = None
        self._watermark: str | None = None
        self._failures: int = 0

    async def start(self) -> None:
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
            for mr in await self._client.list_project_mrs(
                pid, state="opened", updated_after=self._watermark
            ):
                await self._process_mr(pid, mr)
        self._watermark = poll_start

    async def _process_mr(self, project_id: int, mr: MRListItem) -> None:
        key = f"review:{project_id}:{mr.iid}:{mr.sha}"
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
