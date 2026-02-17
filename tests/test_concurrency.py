"""Tests for concurrency primitives."""

import asyncio

from gitlab_copilot_agent.concurrency import (
    DeduplicationStore,
    DistributedLock,
    MemoryDedup,
    MemoryLock,
    ProcessedIssueTracker,
    RepoLockManager,
    ReviewedMRTracker,
)
from tests.conftest import EXAMPLE_CLONE_URL, PROJECT_ID

REPO_URL_A = "https://a.example.com/group/repo-a.git"
REPO_URL_B = "https://b.example.com/group/repo-b.git"
REPO_URL_1 = "https://gitlab.example.com/group/repo1.git"
REPO_URL_2 = "https://gitlab.example.com/group/repo2.git"
REPO_URL_3 = "https://gitlab.example.com/group/repo3.git"
REPO_URL_4 = "https://gitlab.example.com/group/repo4.git"
ISSUE_KEY_1 = "KAN-1"
ISSUE_KEY_2 = "KAN-2"
HEAD_SHA_1 = "abc123"
HEAD_SHA_2 = "def456"
HEAD_SHA_99 = "sha99"
MR_IID_7 = 7
MR_IID_8 = 8
MR_IID_99 = 99


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


async def test_processed_issue_tracker() -> None:
    tracker = ProcessedIssueTracker()
    assert not tracker.is_processed(ISSUE_KEY_1)
    tracker.mark(ISSUE_KEY_1)
    assert tracker.is_processed(ISSUE_KEY_1)
    assert not tracker.is_processed(ISSUE_KEY_2)


async def test_processed_issue_tracker_evicts_when_max_size_exceeded() -> None:
    """Test that ProcessedIssueTracker evicts oldest half when max_size exceeded."""
    tracker = ProcessedIssueTracker(max_size=10)

    for i in range(10):
        tracker.mark(f"ISSUE-{i}")

    assert len(tracker._processed) == 10

    tracker.mark("ISSUE-10")

    assert len(tracker) == 5

    for i in range(6):
        assert not tracker.is_processed(f"ISSUE-{i}")

    for i in range(6, 11):
        assert tracker.is_processed(f"ISSUE-{i}")


async def test_processed_issue_tracker_preserves_insertion_order() -> None:
    """Test that ProcessedIssueTracker maintains insertion order for eviction."""
    tracker = ProcessedIssueTracker(max_size=6)

    for i in range(6):
        tracker.mark(f"ISSUE-{i}")

    tracker.mark("ISSUE-6")

    assert len(tracker) == 3

    assert not tracker.is_processed("ISSUE-0")
    assert not tracker.is_processed("ISSUE-1")
    assert not tracker.is_processed("ISSUE-2")
    assert not tracker.is_processed("ISSUE-3")

    assert tracker.is_processed("ISSUE-4")
    assert tracker.is_processed("ISSUE-5")
    assert tracker.is_processed("ISSUE-6")


# -- ReviewedMRTracker tests --


async def test_reviewed_mr_tracker_marks_and_checks() -> None:
    tracker = ReviewedMRTracker()
    assert not tracker.is_reviewed(PROJECT_ID, MR_IID_7, HEAD_SHA_1)
    tracker.mark(PROJECT_ID, MR_IID_7, HEAD_SHA_1)
    assert tracker.is_reviewed(PROJECT_ID, MR_IID_7, HEAD_SHA_1)
    # Same MR, different SHA → not reviewed
    assert not tracker.is_reviewed(PROJECT_ID, MR_IID_7, HEAD_SHA_2)
    # Same SHA, different MR → not reviewed
    assert not tracker.is_reviewed(PROJECT_ID, MR_IID_8, HEAD_SHA_1)
    # Same SHA, different project → not reviewed
    assert not tracker.is_reviewed(99, MR_IID_7, HEAD_SHA_1)


async def test_reviewed_mr_tracker_evicts_when_full() -> None:
    tracker = ReviewedMRTracker(max_size=4)
    for i in range(4):
        tracker.mark(1, i, f"sha{i}")
    assert len(tracker) == 4

    tracker.mark(1, MR_IID_99, HEAD_SHA_99)
    assert len(tracker) == 2
    assert not tracker.is_reviewed(1, 0, "sha0")
    assert not tracker.is_reviewed(1, 1, "sha1")
    assert tracker.is_reviewed(1, 3, "sha3")
    assert tracker.is_reviewed(1, MR_IID_99, HEAD_SHA_99)


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
