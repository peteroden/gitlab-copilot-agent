"""In-memory implementations of concurrency protocols for local execution and testing."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.concurrency.protocols import QueueMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = structlog.get_logger()

_DEFAULT_MAX_LOCKS = 1024
_DEFAULT_MAX_PROCESSED = 10_000


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


class MemoryTaskQueue:
    """In-memory TaskQueue for local executor and testing."""

    def __init__(self) -> None:
        self._messages: list[QueueMessage] = []
        self._counter: int = 0
        self._blobs: dict[str, bytes] = {}

    async def enqueue(self, task_id: str, payload: str) -> None:
        self._counter += 1
        msg = QueueMessage(
            message_id=str(self._counter),
            receipt=str(self._counter),
            task_id=task_id,
            payload=payload,
            dequeue_count=1,
        )
        self._messages.append(msg)

    async def dequeue(self, visibility_timeout: int = 300) -> QueueMessage | None:
        return self._messages.pop(0) if self._messages else None

    async def complete(self, message: QueueMessage) -> None:
        """No-op — message already consumed by dequeue."""

    async def upload_blob(self, name: str, data: bytes) -> None:
        self._blobs[name] = data

    async def download_blob(self, name: str) -> bytes:
        if name not in self._blobs:
            raise KeyError(f"Blob not found: {name}")
        return self._blobs[name]

    async def aclose(self) -> None:
        self._messages.clear()
        self._blobs.clear()


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
            self._locks.move_to_end(key)

        async with self._locks[key]:
            yield

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
