"""Per-repo locking, Jira issue deduplication, and MR review deduplication."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, runtime_checkable

import structlog

log = structlog.get_logger()

_DEFAULT_MAX_LOCKS = 1024
_DEFAULT_MAX_PROCESSED = 10_000


@runtime_checkable
class DistributedLock(Protocol):
    """Protocol for distributed locking backends."""

    def acquire(self, key: str, ttl_seconds: int = 300) -> AbstractAsyncContextManager[None]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class DeduplicationStore(Protocol):
    """Protocol for deduplication backends."""

    async def is_seen(self, key: str) -> bool: ...
    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ResultStore(Protocol):
    """Protocol for task result storage backends."""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int = 3600) -> None: ...

    async def aclose(self) -> None: ...


class MemoryResultStore:
    """In-memory result store for local executor and testing."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str, ttl: int = 3600) -> None:
        self._data[key] = value

    async def aclose(self) -> None:
        self._data.clear()


class MemoryLock:
    """Async lock per key — serializes operations on the same key.

    Uses LRU eviction to prevent unbounded memory growth when max_size is exceeded.
    Locked entries are never evicted. Implements DistributedLock protocol.
    """

    def __init__(self, max_size: int = _DEFAULT_MAX_LOCKS) -> None:
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._max_size = max_size

    def _evict_unlocked(self) -> None:
        """Evict oldest unlocked entries until within max_size."""
        if len(self._locks) <= self._max_size:
            return

        to_evict: list[str] = []
        for key, lock in self._locks.items():
            if len(self._locks) - len(to_evict) <= self._max_size:
                break
            if not lock.locked():
                to_evict.append(key)

        for key in to_evict:
            del self._locks[key]

        if to_evict:
            log.warning(
                "repo_lock_eviction",
                evicted_count=len(to_evict),
                max_size=self._max_size,
                current_size=len(self._locks),
            )

    @asynccontextmanager
    async def acquire(self, key: str, ttl_seconds: int = 300) -> AsyncIterator[None]:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        else:
            # Move to end (LRU)
            self._locks.move_to_end(key)

        async with self._locks[key]:
            yield

        # Evict after release
        self._evict_unlocked()

    def __len__(self) -> int:
        return len(self._locks)

    async def aclose(self) -> None:
        """No-op — in-memory locks need no cleanup."""


# Backward compatibility alias
RepoLockManager = MemoryLock


class MemoryDedup:
    """In-memory deduplication store implementing DeduplicationStore protocol.

    Uses size-based eviction to prevent unbounded memory growth.
    """

    def __init__(self, max_size: int = _DEFAULT_MAX_PROCESSED) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def _evict_if_needed(self) -> None:
        if len(self._seen) <= self._max_size:
            return
        target_size = self._max_size // 2
        evict_count = len(self._seen) - target_size
        for _ in range(evict_count):
            self._seen.popitem(last=False)
        log.warning(
            "dedup_store_eviction",
            evicted_count=evict_count,
            max_size=self._max_size,
            current_size=len(self._seen),
        )

    async def is_seen(self, key: str) -> bool:
        return key in self._seen

    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None:
        self._seen[key] = None
        self._evict_if_needed()

    def __len__(self) -> int:
        return len(self._seen)

    async def aclose(self) -> None:
        """No-op — in-memory store needs no cleanup."""


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


class ReviewedMRTracker:
    """Track reviewed (project_id, mr_iid, head_sha) tuples to avoid duplicate reviews.

    In-memory only — a service restart allows re-review, which is acceptable.
    Uses size-based eviction identical to ProcessedIssueTracker.
    """

    def __init__(self, max_size: int = _DEFAULT_MAX_PROCESSED) -> None:
        self._reviewed: OrderedDict[tuple[int, int, str], None] = OrderedDict()
        self._max_size = max_size

    def _evict_if_needed(self) -> None:
        if len(self._reviewed) <= self._max_size:
            return
        target_size = self._max_size // 2
        evict_count = len(self._reviewed) - target_size
        for _ in range(evict_count):
            self._reviewed.popitem(last=False)
        log.warning(
            "reviewed_mr_eviction",
            evicted_count=evict_count,
            max_size=self._max_size,
            current_size=len(self._reviewed),
        )

    def is_reviewed(self, project_id: int, mr_iid: int, head_sha: str) -> bool:
        return (project_id, mr_iid, head_sha) in self._reviewed

    def mark(self, project_id: int, mr_iid: int, head_sha: str) -> None:
        self._reviewed[(project_id, mr_iid, head_sha)] = None
        self._evict_if_needed()

    def __len__(self) -> int:
        return len(self._reviewed)
