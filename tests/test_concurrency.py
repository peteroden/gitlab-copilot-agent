"""Tests for concurrency primitives."""

import asyncio

from gitlab_copilot_agent.concurrency import (
    DeduplicationStore,
    DistributedLock,
    MemoryDedup,
    MemoryLock,
    MemoryTaskQueue,
    RepoLockManager,
    TaskQueue,
)
from tests.conftest import EXAMPLE_CLONE_URL

REPO_URL_A = "https://a.example.com/group/repo-a.git"
REPO_URL_B = "https://b.example.com/group/repo-b.git"
REPO_URL_1 = "https://gitlab.example.com/group/repo1.git"
REPO_URL_2 = "https://gitlab.example.com/group/repo2.git"
REPO_URL_3 = "https://gitlab.example.com/group/repo3.git"
REPO_URL_4 = "https://gitlab.example.com/group/repo4.git"


# -- Protocol conformance tests --


async def test_memory_lock_implements_distributed_lock() -> None:
    assert isinstance(MemoryLock(), DistributedLock)


async def test_memory_dedup_implements_deduplication_store() -> None:
    assert isinstance(MemoryDedup(), DeduplicationStore)


async def test_backward_compat_alias() -> None:
    assert RepoLockManager is MemoryLock


# -- MemoryLock tests (formerly RepoLockManager) --


async def test_repo_lock_serializes_same_repo() -> None:
    locks = RepoLockManager()
    order: list[int] = []

    async def task(n: int) -> None:
        async with locks.acquire(EXAMPLE_CLONE_URL):
            order.append(n)
            await asyncio.sleep(0.05)

    await asyncio.gather(task(1), task(2))
    assert len(order) == 2


async def test_repo_lock_allows_parallel_different_repos() -> None:
    locks = RepoLockManager()
    started: list[str] = []

    async def task(url: str) -> None:
        async with locks.acquire(url):
            started.append(url)
            await asyncio.sleep(0.05)

    await asyncio.gather(task(REPO_URL_A), task(REPO_URL_B))
    assert len(started) == 2


async def test_repo_lock_evicts_when_max_size_exceeded() -> None:
    """Test that RepoLockManager evicts oldest unlocked entries when max_size exceeded."""
    locks = RepoLockManager(max_size=3)

    async with locks.acquire(REPO_URL_1):
        pass
    async with locks.acquire(REPO_URL_2):
        pass
    async with locks.acquire(REPO_URL_3):
        pass

    assert len(locks) == 3

    async with locks.acquire(REPO_URL_4):
        pass

    assert len(locks) == 3
    assert REPO_URL_1 not in locks._locks
    assert REPO_URL_4 in locks._locks


async def test_repo_lock_does_not_evict_locked_entries() -> None:
    """Test that locked entries are never evicted, even when max_size exceeded."""
    locks = RepoLockManager(max_size=2)

    async with locks.acquire(REPO_URL_1):
        async with locks.acquire(REPO_URL_2):
            pass

        async with locks.acquire(REPO_URL_3):
            pass

        assert REPO_URL_1 in locks._locks
        assert REPO_URL_2 not in locks._locks
        assert REPO_URL_3 in locks._locks


async def test_repo_lock_lru_behavior() -> None:
    """Test that RepoLockManager uses LRU (moves accessed items to end)."""
    locks = RepoLockManager(max_size=3)

    async with locks.acquire(REPO_URL_1):
        pass
    async with locks.acquire(REPO_URL_2):
        pass
    async with locks.acquire(REPO_URL_3):
        pass

    # Access repo1 again (moves it to end)
    async with locks.acquire(REPO_URL_1):
        pass

    # Add repo4 — should evict repo2 (oldest), not repo1 (recently used)
    async with locks.acquire(REPO_URL_4):
        pass

    assert REPO_URL_1 in locks._locks
    assert REPO_URL_2 not in locks._locks
    assert REPO_URL_3 in locks._locks
    assert REPO_URL_4 in locks._locks


# -- MemoryDedup tests --


async def test_memory_dedup_marks_and_checks() -> None:
    store = MemoryDedup()
    assert not await store.is_seen("key-1")
    await store.mark_seen("key-1")
    assert await store.is_seen("key-1")
    assert not await store.is_seen("key-2")


async def test_memory_dedup_evicts_when_full() -> None:
    store = MemoryDedup(max_size=4)
    for i in range(4):
        await store.mark_seen(f"k-{i}")
    assert len(store) == 4

    await store.mark_seen("k-99")
    assert len(store) == 2
    assert not await store.is_seen("k-0")
    assert not await store.is_seen("k-1")
    assert await store.is_seen("k-3")
    assert await store.is_seen("k-99")


# --- MemoryTaskQueue tests ---

TASK_ID_1 = "task-abc-123"
TASK_ID_2 = "task-def-456"
TASK_PAYLOAD = '{"repo_url": "https://gitlab.example.com/g/r.git"}'


async def test_memory_task_queue_enqueue_dequeue_roundtrip() -> None:
    queue = MemoryTaskQueue()
    await queue.enqueue(TASK_ID_1, TASK_PAYLOAD)
    msg = await queue.dequeue()
    assert msg is not None
    assert msg.task_id == TASK_ID_1
    assert msg.payload == TASK_PAYLOAD
    assert msg.dequeue_count == 1


async def test_memory_task_queue_dequeue_empty_returns_none() -> None:
    queue = MemoryTaskQueue()
    assert await queue.dequeue() is None


async def test_memory_task_queue_fifo_ordering() -> None:
    queue = MemoryTaskQueue()
    await queue.enqueue(TASK_ID_1, "first")
    await queue.enqueue(TASK_ID_2, "second")
    msg1 = await queue.dequeue()
    msg2 = await queue.dequeue()
    assert msg1 is not None and msg1.task_id == TASK_ID_1
    assert msg2 is not None and msg2.task_id == TASK_ID_2


async def test_memory_task_queue_complete_is_noop() -> None:
    queue = MemoryTaskQueue()
    await queue.enqueue(TASK_ID_1, TASK_PAYLOAD)
    msg = await queue.dequeue()
    assert msg is not None
    await queue.complete(msg)  # should not raise
    assert await queue.dequeue() is None  # message was consumed


async def test_memory_task_queue_satisfies_protocol() -> None:
    assert isinstance(MemoryTaskQueue(), TaskQueue)
