"""Per-repo locking, Jira issue deduplication, and MR review deduplication."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, runtime_checkable

import structlog

log = structlog.get_logger()

_DEFAULT_MAX_LOCKS = 1024
_DEFAULT_MAX_PROCESSED = 10_000


@runtime_checkable
class DistributedLock(Protocol):
    """Protocol for distributed locking.

    Implementations provide async context managers for acquiring locks on arbitrary keys.
    The ttl_seconds parameter allows backends like Redis to set expiration (in-memory
    implementations may ignore it).
    """

    @asynccontextmanager
    async def acquire(self, key: str, ttl_seconds: int = 300) -> AsyncIterator[None]:
        """Acquire lock on key with optional TTL."""
        ...


@runtime_checkable
class DeduplicationStore(Protocol):
    """Protocol for deduplication state.

    Tracks whether keys have been seen before. Implementations may optionally
    support TTL-based expiration.
    """

    async def is_seen(self, key: str) -> bool:
        """Check if key has been seen."""
        ...

    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None:
        """Mark key as seen with optional TTL."""
        ...


class MemoryLock:
    """In-memory distributed lock implementation.

    Async lock per key with LRU eviction. Locked entries are never evicted.
    Implements DistributedLock protocol (ttl_seconds is ignored in memory).
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
                "lock_eviction",
                evicted_count=len(to_evict),
                max_size=self._max_size,
                current_size=len(self._locks),
            )

    @asynccontextmanager
    async def acquire(self, key: str, ttl_seconds: int = 300) -> AsyncIterator[None]:
        """Acquire lock on key. ttl_seconds is ignored in memory implementation."""
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


# Backward compatibility alias
RepoLockManager = MemoryLock


class MemoryDedup:
    """In-memory deduplication store implementation.

    Tracks seen keys with size-based eviction (oldest half evicted when max_size exceeded).
    Implements DeduplicationStore protocol (ttl_seconds is ignored in memory).
    """

    def __init__(self, max_size: int = _DEFAULT_MAX_PROCESSED) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def _evict_if_needed(self) -> None:
        """Clear oldest half of entries when max_size is exceeded."""
        if len(self._seen) <= self._max_size:
            return

        target_size = self._max_size // 2
        evict_count = len(self._seen) - target_size

        for _ in range(evict_count):
            self._seen.popitem(last=False)

        log.warning(
            "dedup_eviction",
            evicted_count=evict_count,
            max_size=self._max_size,
            current_size=len(self._seen),
        )

    async def is_seen(self, key: str) -> bool:
        """Check if key has been seen."""
        return key in self._seen

    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None:
        """Mark key as seen. ttl_seconds is ignored in memory implementation."""
        self._seen[key] = None
        self._evict_if_needed()

    def __len__(self) -> int:
        return len(self._seen)


class ProcessedIssueTracker:
    """Track processed Jira issue keys to avoid re-processing within a run.

    Thin wrapper around MemoryDedup for backward compatibility.
    Uses size-based eviction to prevent unbounded memory growth when max_size is exceeded.
    """

    def __init__(self, max_size: int = _DEFAULT_MAX_PROCESSED) -> None:
        self._store = MemoryDedup(max_size=max_size)
        # For backward compat, expose internal state for tests
        self._processed = self._store._seen
        self._max_size = max_size

    def _evict_if_needed(self) -> None:
        """Clear oldest half of entries when max_size is exceeded."""
        self._store._evict_if_needed()

    def is_processed(self, key: str) -> bool:
        # Synchronous wrapper for backward compat
        return key in self._store._seen

    def mark(self, key: str) -> None:
        # Synchronous wrapper for backward compat
        self._store._seen[key] = None
        self._store._evict_if_needed()

    def __len__(self) -> int:
        return len(self._store)



class ReviewedMRTracker:
    """Track reviewed (project_id, mr_iid, head_sha) tuples to avoid duplicate reviews.

    In-memory only — a service restart allows re-review, which is acceptable.
    Uses size-based eviction identical to ProcessedIssueTracker.

    Note: This maintains the tuple-based API for backward compatibility but internally
    converts to string keys for protocol compatibility.
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

