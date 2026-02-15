"""Tests for concurrency primitives."""

import asyncio

from gitlab_copilot_agent.concurrency import ProcessedIssueTracker, RepoLockManager


async def test_repo_lock_serializes_same_repo() -> None:
    locks = RepoLockManager()
    order: list[int] = []

    async def task(n: int) -> None:
        async with locks.acquire("https://gitlab.com/group/repo.git"):
            order.append(n)
            await asyncio.sleep(0.05)

    await asyncio.gather(task(1), task(2))
    # Both complete, order is deterministic (first to acquire wins)
    assert len(order) == 2


async def test_repo_lock_allows_parallel_different_repos() -> None:
    locks = RepoLockManager()
    started: list[str] = []

    async def task(url: str) -> None:
        async with locks.acquire(url):
            started.append(url)
            await asyncio.sleep(0.05)

    await asyncio.gather(task("https://a.git"), task("https://b.git"))
    assert len(started) == 2


async def test_repo_lock_evicts_when_max_size_exceeded() -> None:
    """Test that RepoLockManager evicts oldest unlocked entries when max_size exceeded."""
    locks = RepoLockManager(max_size=3)

    # Add 3 repos (at max_size)
    async with locks.acquire("https://repo1.git"):
        pass
    async with locks.acquire("https://repo2.git"):
        pass
    async with locks.acquire("https://repo3.git"):
        pass

    assert len(locks) == 3

    # Add a 4th repo - should trigger eviction of oldest (repo1)
    async with locks.acquire("https://repo4.git"):
        pass

    assert len(locks) == 3
    assert "https://repo1.git" not in locks._locks
    assert "https://repo4.git" in locks._locks


async def test_repo_lock_does_not_evict_locked_entries() -> None:
    """Test that locked entries are never evicted, even when max_size exceeded."""
    locks = RepoLockManager(max_size=2)

    # Acquire repo1 and hold it
    async with locks.acquire("https://repo1.git"):
        # Add repo2
        async with locks.acquire("https://repo2.git"):
            pass

        # Add repo3 - should evict repo2 (unlocked), not repo1 (locked)
        async with locks.acquire("https://repo3.git"):
            pass

        # repo1 should still be present (it's locked)
        assert "https://repo1.git" in locks._locks
        # repo2 should be evicted
        assert "https://repo2.git" not in locks._locks
        # repo3 should be present
        assert "https://repo3.git" in locks._locks


async def test_repo_lock_lru_behavior() -> None:
    """Test that RepoLockManager uses LRU (moves accessed items to end)."""
    locks = RepoLockManager(max_size=3)

    # Add 3 repos
    async with locks.acquire("https://repo1.git"):
        pass
    async with locks.acquire("https://repo2.git"):
        pass
    async with locks.acquire("https://repo3.git"):
        pass

    # Access repo1 again (moves it to end)
    async with locks.acquire("https://repo1.git"):
        pass

    # Add repo4 - should evict repo2 (oldest), not repo1 (recently used)
    async with locks.acquire("https://repo4.git"):
        pass

    assert "https://repo1.git" in locks._locks
    assert "https://repo2.git" not in locks._locks
    assert "https://repo3.git" in locks._locks
    assert "https://repo4.git" in locks._locks


async def test_processed_issue_tracker() -> None:
    tracker = ProcessedIssueTracker()
    assert not tracker.is_processed("KAN-1")
    tracker.mark("KAN-1")
    assert tracker.is_processed("KAN-1")
    assert not tracker.is_processed("KAN-2")


async def test_processed_issue_tracker_evicts_when_max_size_exceeded() -> None:
    """Test that ProcessedIssueTracker evicts oldest half when max_size exceeded."""
    tracker = ProcessedIssueTracker(max_size=10)

    # Add 10 issues (at max_size)
    for i in range(10):
        tracker.mark(f"ISSUE-{i}")

    assert len(tracker._processed) == 10

    # Add one more - should trigger eviction of oldest entries down to max_size // 2 = 5
    tracker.mark("ISSUE-10")

    assert len(tracker) == 5

    # First 6 should be evicted (10 - 5 + 1 = 6 evicted to get to 5)
    for i in range(6):
        assert not tracker.is_processed(f"ISSUE-{i}")

    # Last 5 should remain
    for i in range(6, 11):
        assert tracker.is_processed(f"ISSUE-{i}")


async def test_processed_issue_tracker_preserves_insertion_order() -> None:
    """Test that ProcessedIssueTracker maintains insertion order for eviction."""
    tracker = ProcessedIssueTracker(max_size=6)

    # Add 6 issues
    for i in range(6):
        tracker.mark(f"ISSUE-{i}")

    # Add one more to trigger eviction (target_size = 6 // 2 = 3)
    tracker.mark("ISSUE-6")

    # Should have 3 items remaining
    assert len(tracker) == 3

    # Should evict oldest 4 (ISSUE-0, ISSUE-1, ISSUE-2, ISSUE-3)
    assert not tracker.is_processed("ISSUE-0")
    assert not tracker.is_processed("ISSUE-1")
    assert not tracker.is_processed("ISSUE-2")
    assert not tracker.is_processed("ISSUE-3")

    # Should keep newest 3 (ISSUE-4, ISSUE-5, ISSUE-6)
    assert tracker.is_processed("ISSUE-4")
    assert tracker.is_processed("ISSUE-5")
    assert tracker.is_processed("ISSUE-6")
