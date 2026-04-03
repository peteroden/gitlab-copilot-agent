"""Background GitLab poller — discovers new/updated MRs and @mention notes."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import structlog

from gitlab_copilot_agent.concurrency import DeduplicationStore, DistributedLock
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.credential_registry import CredentialRegistry
from gitlab_copilot_agent.discussion_handler import handle_discussion_interaction
from gitlab_copilot_agent.discussion_models import AgentIdentity
from gitlab_copilot_agent.gitlab_client import (
    GitLabClient,
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
from gitlab_copilot_agent.orchestrator import handle_review
from gitlab_copilot_agent.project_registry import ProjectRegistry
from gitlab_copilot_agent.task_executor import TaskExecutionError, TaskExecutor

log = structlog.get_logger()
_DEDUP_TTL = 86400
_MAX_BACKOFF = 300


class GitLabPoller:
    """Background poller that discovers open MRs and @mention notes."""

    def __init__(
        self,
        gl_client: GitLabClientProtocol,
        settings: Settings,
        project_ids: set[int],
        dedup: DeduplicationStore,
        executor: TaskExecutor,
        repo_locks: DistributedLock | None = None,
        project_registry: ProjectRegistry | None = None,
        credential_registry: CredentialRegistry | None = None,
    ) -> None:
        self._client = gl_client
        self._settings = settings
        self._project_ids = project_ids
        self._dedup = dedup
        self._executor = executor
        self._repo_locks = repo_locks
        self._project_registry = project_registry
        self._credential_registry = credential_registry
        self._project_clients: dict[str, GitLabClientProtocol] = {}
        self._identity_cache: dict[str, AgentIdentity] = {}
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
        # Note watermark starts at "now" to avoid replaying old mentions
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
            try:
                client = self._client_for_project(pid)
                # Fetch ALL open MRs — needed for note scanning since notes
                # don't update MR.updated_at. MR review uses dedup to avoid
                # re-reviewing unchanged MRs.
                all_open_mrs = await client.list_project_mrs(pid, state="opened")
                for mr in all_open_mrs:
                    await self._process_mr(pid, mr)
                await self._process_notes(pid, all_open_mrs, client)
            except Exception as exc:
                ref = "default"
                if self._project_registry is not None:
                    resolved = self._project_registry.get_by_project_id(pid)
                    if resolved is not None:
                        ref = resolved.credential_ref
                await log.aerror(
                    "gitlab_poll_project_error",
                    project_id=pid,
                    credential_ref=ref,
                    error=str(exc),
                    hint="Check that the GitLab token for this credential_ref is valid, "
                    "has api + read_repository scopes, and uses Developer role or higher",
                )
        self._watermark = poll_start
        self._note_watermark = poll_start

    def _resolve_token(self, project_id: int) -> str | None:
        """Return per-project token from registry, or None for global fallback."""
        if self._project_registry is not None:
            resolved = self._project_registry.get_by_project_id(project_id)
            if resolved is not None:
                log.info(
                    "credential_resolved",
                    project_id=project_id,
                    credential_ref=resolved.credential_ref,
                    source="project_registry",
                )
                return resolved.token
        log.info("credential_resolved", project_id=project_id, source="global_fallback")
        return None

    def _client_for_project(self, project_id: int) -> GitLabClientProtocol:
        """Return a per-project GitLabClient if available, else the default client."""
        if self._project_registry is not None:
            resolved = self._project_registry.get_by_project_id(project_id)
            if resolved is not None:
                ref = resolved.credential_ref
                if ref not in self._project_clients:
                    self._project_clients[ref] = GitLabClient(
                        self._settings.gitlab_url, resolved.token
                    )
                return self._project_clients[ref]
        return self._client

    async def _process_mr(self, project_id: int, mr: MRListItem) -> None:
        if mr.sha is None:
            return  # MR has no commits (e.g. empty draft)
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
        try:
            await handle_review(
                self._settings,
                payload,
                self._executor,
                project_token=self._resolve_token(project_id),
            )
        except TaskExecutionError:
            await log.awarning("gitlab_review_task_failed", project_id=project_id, mr_iid=mr.iid)
            await self._dedup.mark_seen(key, ttl_seconds=_DEDUP_TTL)
            return
        await self._dedup.mark_seen(key, ttl_seconds=_DEDUP_TTL)

    async def _resolve_agent_identity(self, project_id: int) -> AgentIdentity | None:
        """Resolve the agent identity for the credential used by this project."""
        if self._credential_registry is None:
            return None
        ref = "default"
        if self._project_registry is not None:
            resolved = self._project_registry.get_by_project_id(project_id)
            if resolved is not None:
                ref = resolved.credential_ref
        # Cache per credential_ref
        if ref in self._identity_cache:
            return self._identity_cache[ref]
        try:
            identity = await self._credential_registry.resolve_identity(
                ref, self._settings.gitlab_url
            )
            self._identity_cache[ref] = identity
            return identity
        except Exception:
            await log.awarning(
                "poller_identity_resolution_failed", credential_ref=ref, exc_info=True
            )
            return None

    async def _process_notes(
        self, project_id: int, mrs: list[MRListItem], client: GitLabClientProtocol
    ) -> None:
        agent_identity = await self._resolve_agent_identity(project_id)
        if agent_identity is None:
            await log.adebug("skipping_notes_no_identity", project_id=project_id)
            return
        mention_pattern = re.compile(
            rf"(?<![.\w-])@{re.escape(agent_identity.username)}(?![.\w-])"
        )
        for mr in mrs:
            notes = await client.list_mr_notes(
                project_id, mr.iid, created_after=self._note_watermark
            )
            if notes:
                await log.ainfo(
                    "notes_found",
                    project_id=project_id,
                    mr_iid=mr.iid,
                    note_count=len(notes),
                    agent_username=agent_identity.username,
                    watermark=self._note_watermark,
                )
            for note in notes:
                if note.system:
                    continue
                if not mention_pattern.search(note.body):
                    continue
                # Skip self-authored notes (consistent with webhook)
                if note.author.id == agent_identity.user_id:
                    continue
                note_key = f"note:{project_id}:{mr.iid}:{note.id}"
                if await self._dedup.is_seen(note_key):
                    continue
                payload = _build_note_payload(note, mr, project_id, self._settings)
                try:
                    await handle_discussion_interaction(
                        self._settings,
                        payload,
                        self._executor,
                        agent_identity,
                        project_token=self._resolve_token(project_id),
                        repo_locks=self._repo_locks,
                    )
                except TaskExecutionError:
                    await log.awarning(
                        "gitlab_mention_note_task_failed",
                        note_id=note.id,
                        project_id=project_id,
                        mr_iid=mr.iid,
                    )
                    await self._dedup.mark_seen(note_key, ttl_seconds=_DEDUP_TTL)
                    continue
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
            id=note.id,
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
