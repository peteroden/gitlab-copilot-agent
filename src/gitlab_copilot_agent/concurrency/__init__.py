"""Concurrency primitives — locking, deduplication, queuing, result storage."""

from gitlab_copilot_agent.concurrency.memory import (
    MemoryDedup,
    MemoryLock,
    MemoryResultStore,
    MemoryTaskQueue,
    RepoLockManager,
)
from gitlab_copilot_agent.concurrency.protocols import (
    DeduplicationStore,
    DistributedLock,
    QueueMessage,
    ResultStore,
    TaskQueue,
)

__all__ = [
    "DeduplicationStore",
    "DistributedLock",
    "MemoryDedup",
    "MemoryLock",
    "MemoryResultStore",
    "MemoryTaskQueue",
    "QueueMessage",
    "RepoLockManager",
    "ResultStore",
    "TaskQueue",
]
