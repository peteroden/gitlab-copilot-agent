"""Unified deduplication service — single interface for all trigger types.

Composes a local in-memory front-cache with a shared backend (memory or
Azure Table).  Review checks hit local first (webhook fast-path immune to
backend failures), then fall through to the shared store.  Note and issue
dedup go directly through the shared backend for cross-node visibility.

Callers decide *when* to call ``mark_*`` — the service imposes no
success/failure policy.
"""

from __future__ import annotations

import structlog

from gitlab_copilot_agent.concurrency import DeduplicationStore, MemoryDedup

log = structlog.get_logger()

_DEFAULT_TTL = 86_400  # 1 day


class DeduplicationService:
    """Unified dedup for review, note, and issue events.

    Args:
        backend: Shared ``DeduplicationStore`` (``MemoryDedup`` or Azure Table).
        review_on_push: When True, review keys include ``head_sha`` for per-push
            dedup.  When False, keys are per-MR (re-review on the next poll
            only after a new commit).
    """

    def __init__(
        self,
        backend: DeduplicationStore,
        *,
        review_on_push: bool = True,
    ) -> None:
        self._backend = backend
        self._local = MemoryDedup()
        self._review_on_push = review_on_push

    # -- Key construction (centralized) ------------------------------------

    def _review_key(self, project_id: int, mr_iid: int, head_sha: str) -> str:
        if self._review_on_push:
            return f"review:{project_id}:{mr_iid}:{head_sha}"
        return f"review:{project_id}:{mr_iid}"

    @staticmethod
    def _note_key(project_id: int, mr_iid: int, note_id: int) -> str:
        return f"note:{project_id}:{mr_iid}:{note_id}"

    # -- Review dedup (local + shared) -------------------------------------

    async def is_review_seen(self, project_id: int, mr_iid: int, head_sha: str) -> bool:
        """Check local cache first (fast), then shared backend.

        Backend errors are treated as a miss (fail-open) to prevent
        backend outages from blocking webhook intake.  Cross-trigger
        dedup is best-effort — the local cache still catches true
        duplicates from same-process redeliveries.
        """
        key = self._review_key(project_id, mr_iid, head_sha)
        if await self._local.is_seen(key):
            return True
        try:
            return await self._backend.is_seen(key)
        except Exception:
            log.warning("dedup_review_backend_error", key=key, exc_info=True)
            return False

    async def mark_review(self, project_id: int, mr_iid: int, head_sha: str) -> None:
        """Mark in both local cache and shared backend."""
        key = self._review_key(project_id, mr_iid, head_sha)
        await self._local.mark_seen(key, ttl_seconds=_DEFAULT_TTL)
        await self._backend.mark_seen(key, ttl_seconds=_DEFAULT_TTL)

    # -- Note dedup (shared backend) ---------------------------------------

    async def is_note_seen(self, project_id: int, mr_iid: int, note_id: int) -> bool:
        """Check shared backend for note dedup."""
        key = self._note_key(project_id, mr_iid, note_id)
        return await self._backend.is_seen(key)

    async def mark_note(self, project_id: int, mr_iid: int, note_id: int) -> None:
        """Mark note as processed in shared backend."""
        key = self._note_key(project_id, mr_iid, note_id)
        await self._backend.mark_seen(key, ttl_seconds=_DEFAULT_TTL)

    # -- Issue dedup (shared backend, cross-node visible) --------------------

    @staticmethod
    def _issue_key(issue_key: str) -> str:
        return f"jira:{issue_key}"

    async def is_issue_seen(self, issue_key: str) -> bool:
        """Check if Jira issue was already processed via shared backend."""
        key = self._issue_key(issue_key)
        return await self._backend.is_seen(key)

    async def mark_issue(self, issue_key: str) -> None:
        """Mark Jira issue as processed in shared backend."""
        key = self._issue_key(issue_key)
        await self._backend.mark_seen(key, ttl_seconds=_DEFAULT_TTL)

    # -- Lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Close both local cache and shared backend."""
        await self._local.aclose()
        await self._backend.aclose()
