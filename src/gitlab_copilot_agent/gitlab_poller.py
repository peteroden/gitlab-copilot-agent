"""Background GitLab poller — discovers new/updated MRs and @mention notes."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import structlog

from gitlab_copilot_agent.concurrency import DistributedLock
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.credential_registry import CredentialRegistry
from gitlab_copilot_agent.dedup import DeduplicationService
from gitlab_copilot_agent.discussion_models import AgentIdentity
from gitlab_copilot_agent.discussion_pipeline import DiscussionContext, DiscussionPipeline
from gitlab_copilot_agent.events import TaskEvent
from gitlab_copilot_agent.gitlab_client import (
    GitLabClient,
    GitLabClientProtocol,
    MRListItem,
)
from gitlab_copilot_agent.pipeline import run_pipeline
from gitlab_copilot_agent.project_registry import ProjectRegistry
from gitlab_copilot_agent.review_pipeline import ReviewContext, ReviewPipeline
from gitlab_copilot_agent.task_executor import TaskExecutionError, TaskExecutor
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)
_DEDUP_TTL = 86400
_MAX_BACKOFF = 300


class GitLabPoller:
    """Background poller that discovers open MRs and @mention notes."""

    def __init__(
        self,
        gl_client: GitLabClientProtocol,
        settings: Settings,
        project_ids: set[int],
        dedup: DeduplicationService,
        executor: TaskExecutor,
        repo_locks: DistributedLock | None = None,
        project_registry: ProjectRegistry | None = None,
        credential_registry: CredentialRegistry | None = None,
        *,
        poll_interval: int = 30,
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
        self._interval: int = poll_interval
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
        """Cancel the polling loop."""
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def update_project_registry(self, registry: ProjectRegistry | None) -> None:
        """Swap the project registry and clear cached per-project clients.

        Called by ``/config/reload`` to hot-swap the registry without
        restarting the poller.
        """
        self._project_registry = registry
        self._project_clients.clear()

    def status(self) -> dict[str, object]:
        """Return health-check status for the poller."""
        return {
            "running": self._task is not None and not self._task.done(),
            "failures": self._failures,
            "watermark": self._watermark,
        }

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
                self._failures = 0
            except Exception:
                self._failures += 1
                await log.aexception("gitlab_poll_error")
            sleep_seconds = min(self._interval * 2**self._failures, _MAX_BACKOFF)
            if self._failures > 0:
                await log.awarning(
                    "gitlab_poll_backoff",
                    failures=self._failures,
                    sleep_seconds=sleep_seconds,
                )
            await asyncio.sleep(sleep_seconds)

    async def _poll_once(self) -> None:
        poll_start = datetime.now(UTC).isoformat()
        await log.ainfo("gitlab_poll_cycle", project_count=len(self._project_ids))
        all_succeeded = True
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
                all_succeeded = False
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
        # Only advance watermarks if all projects were scanned successfully.
        # Otherwise, notes from failed projects would be skipped on the next cycle.
        if all_succeeded:
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
        if await self._dedup.is_review_seen(project_id, mr.iid, mr.sha):
            return
        # Extract namespace from web_url: https://gitlab.example.com/group/project/-/merge_requests/1
        project_url = mr.web_url.split("/-/")[0]
        ns = project_url.removeprefix(self._settings.gitlab_url).strip("/")
        clone_url = f"{project_url}.git"

        credential_ref = "default"
        resolution_behavior = self._settings.resolution_behavior
        if self._project_registry is not None:
            resolved = self._project_registry.get_by_project_id(project_id)
            if resolved is not None:
                credential_ref = resolved.credential_ref
                resolution_behavior = resolved.resolution_behavior

        token = self._resolve_token(project_id) or self._settings.gitlab_token
        event = TaskEvent(
            task_type="review",
            project_id=project_id,
            repo=ns,
            clone_url=clone_url,
            branch=mr.source_branch,
            target_branch=mr.target_branch,
            mr_iid=mr.iid,
            head_sha=mr.sha,
            trigger_source="gitlab_poller",
            token=token,
            credential_ref=credential_ref,
            resolution_behavior=resolution_behavior,
        )
        try:
            with _tracer.start_as_current_span(
                "mr.review",
                attributes={"project_id": event.project_id, "mr_iid": event.mr_iid or 0},
            ):
                gl_client = GitLabClient(self._settings.gitlab_url, event.token)
                pipeline = ReviewPipeline(
                    settings=self._settings,
                    event=event,
                    executor=self._executor,
                    gl_client=gl_client,
                    credential_registry=self._credential_registry,
                )
                await run_pipeline(pipeline, ReviewContext())
        except TaskExecutionError:
            await log.awarning("gitlab_review_task_failed", project_id=project_id, mr_iid=mr.iid)
            await self._dedup.mark_review(project_id, mr.iid, mr.sha)
            return
        await self._dedup.mark_review(project_id, mr.iid, mr.sha)

    async def _resolve_agent_identity(self, project_id: int) -> AgentIdentity | None:
        """Resolve the agent identity for the credential used by this project.

        Delegates to CredentialRegistry which owns the TTL-based cache.
        """
        if self._credential_registry is None:
            return None
        ref = "default"
        if self._project_registry is not None:
            resolved = self._project_registry.get_by_project_id(project_id)
            if resolved is not None:
                ref = resolved.credential_ref
        try:
            return await self._credential_registry.resolve_identity(ref, self._settings.gitlab_url)
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

        # Resolve per-project settings once
        credential_ref = "default"
        resolution_behavior = self._settings.resolution_behavior
        if self._project_registry is not None:
            resolved = self._project_registry.get_by_project_id(project_id)
            if resolved is not None:
                credential_ref = resolved.credential_ref
                resolution_behavior = resolved.resolution_behavior
        token = self._resolve_token(project_id) or self._settings.gitlab_token

        for mr in mrs:
            if mr.sha is None:
                continue
            # Use discussions API to get thread structure for participation detection
            try:
                discussions = await client.list_mr_discussions(project_id, mr.iid)
            except Exception:
                await log.awarning("discussion_fetch_failed", project_id=project_id, mr_iid=mr.iid)
                continue

            # Extract namespace from web_url
            project_url = mr.web_url.split("/-/")[0]
            ns = project_url.removeprefix(self._settings.gitlab_url).strip("/")
            clone_url = f"{project_url}.git"

            for disc in discussions:
                if disc.is_resolved:
                    continue
                # Find the latest non-system note in the thread
                latest_note = None
                for note in reversed(disc.notes):
                    if not note.is_system:
                        latest_note = note
                        break
                if latest_note is None:
                    continue
                # Skip if the latest note is from the agent (we already replied)
                if latest_note.author_id == agent_identity.user_id:
                    continue
                # Skip notes older than the watermark
                if self._note_watermark and latest_note.created_at <= self._note_watermark:
                    continue

                # Check if this note is directed at the agent:
                # 1. @mention in the note body, OR
                # 2. Agent previously participated in this discussion thread
                is_mention = bool(mention_pattern.search(latest_note.body))
                agent_participated = any(n.author_id == agent_identity.user_id for n in disc.notes)
                if not is_mention and not agent_participated:
                    continue

                if await self._dedup.is_note_seen(project_id, mr.iid, latest_note.note_id):
                    continue

                event = TaskEvent(
                    task_type="discussion",
                    project_id=project_id,
                    repo=ns,
                    clone_url=clone_url,
                    branch=mr.source_branch,
                    target_branch=mr.target_branch,
                    mr_iid=mr.iid,
                    trigger_source="gitlab_poller",
                    token=token,
                    credential_ref=credential_ref,
                    resolution_behavior=resolution_behavior,
                    note_id=latest_note.note_id,
                    discussion_id=disc.discussion_id,
                    note_body=latest_note.body,
                )

                try:
                    with _tracer.start_as_current_span(
                        "mr.discussion_interaction",
                        attributes={
                            "project_id": event.project_id,
                            "mr_iid": event.mr_iid or 0,
                        },
                    ):
                        gl_client = GitLabClient(self._settings.gitlab_url, event.token)
                        pipeline = DiscussionPipeline(
                            settings=self._settings,
                            event=event,
                            executor=self._executor,
                            gl_client=gl_client,
                            agent_identity=agent_identity,
                        )
                        ctx = DiscussionContext()
                        if self._repo_locks:
                            async with self._repo_locks.acquire(event.clone_url):
                                await run_pipeline(pipeline, ctx)
                        else:
                            await run_pipeline(pipeline, ctx)
                except TaskExecutionError:
                    await log.awarning(
                        "gitlab_mention_note_task_failed",
                        note_id=latest_note.note_id,
                        project_id=project_id,
                        mr_iid=mr.iid,
                    )
                    await self._dedup.mark_note(project_id, mr.iid, latest_note.note_id)
                    continue
                await self._dedup.mark_note(project_id, mr.iid, latest_note.note_id)
