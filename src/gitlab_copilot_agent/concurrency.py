"""Per-repo locking and Jira issue deduplication."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog

log = structlog.get_logger()

_DEFAULT_MAX_LOCKS = 1024
_DEFAULT_MAX_PROCESSED = 10_000


class RepoLockManager:
    """Async lock per repo URL — serializes operations on the same repo.

    Uses LRU eviction to prevent unbounded memory growth when max_size is exceeded.
    Locked entries are never evicted.
    """

    def __init__(self, max_size: int = _DEFAULT_MAX_LOCKS) -> None:
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._max_size = max_size

    def _evict_unlocked(self) -> None:
        """Evict oldest unlocked entries until within max_size."""
        if len(self._locks) <= self._max_size:
            return

        to_evict = []
        for repo_url, lock in self._locks.items():
            if len(self._locks) - len(to_evict) <= self._max_size:
                break
            if not lock.locked():
                to_evict.append(repo_url)

        for repo_url in to_evict:
            del self._locks[repo_url]

        if to_evict:
            log.warning(
                "repo_lock_eviction",
                evicted_count=len(to_evict),
                max_size=self._max_size,
                current_size=len(self._locks),
            )

    @asynccontextmanager
    async def acquire(self, repo_url: str) -> AsyncIterator[None]:
        if repo_url not in self._locks:
            self._locks[repo_url] = asyncio.Lock()
        else:
            # Move to end (LRU)
            self._locks.move_to_end(repo_url)

        async with self._locks[repo_url]:
            yield

        # Evict after release
        self._evict_unlocked()

    def __len__(self) -> int:
        return len(self._locks)


class ProcessedIssueTracker:
    """Track processed Jira issue keys to avoid re-processing within a run.

    Uses size-based eviction to prevent unbounded memory growth when max_size is exceeded.
    """

    def __init__(self, max_size: int = _DEFAULT_MAX_PROCESSED) -> None:
        self._processed: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def _evict_if_needed(self) -> None:
        """Clear oldest half of entries when max_size is exceeded."""
        if len(self._processed) <= self._max_size:
            return

        target_size = self._max_size // 2
        evict_count = len(self._processed) - target_size

        for _ in range(evict_count):
            self._processed.popitem(last=False)

        log.warning(
            "processed_issue_eviction",
            evicted_count=evict_count,
            max_size=self._max_size,
            current_size=len(self._processed),
        )

    def is_processed(self, key: str) -> bool:
        return key in self._processed

    def mark(self, key: str) -> None:
        self._processed[key] = None
        self._evict_if_needed()

    def __len__(self) -> int:
        return len(self._processed)
