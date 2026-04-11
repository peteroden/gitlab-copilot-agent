"""Protocol definitions for distributed locking, deduplication, result storage, and task queues."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager


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


@dataclass(frozen=True)
class QueueMessage:
    """Handle for a dequeued message. message_id and receipt are opaque."""

    message_id: str
    receipt: str
    task_id: str
    payload: str
    dequeue_count: int


@runtime_checkable
class TaskQueue(Protocol):
    """Enqueue tasks for async workers; dequeue on the worker side.

    Implementations handle the Claim Check pattern transparently.
    """

    async def enqueue(self, task_id: str, payload: str) -> None: ...

    async def dequeue(self, visibility_timeout: int = 300) -> QueueMessage | None:
        """Retrieve the next message, or None if empty."""
        ...

    async def complete(self, message: QueueMessage) -> None:
        """Acknowledge processing. Deletes the queue message."""
        ...

    async def upload_blob(self, name: str, data: bytes) -> None:
        """Upload arbitrary binary data to the blob container."""
        ...

    async def download_blob(self, name: str) -> bytes:
        """Download binary data from the blob container."""
        ...

    async def aclose(self) -> None: ...
