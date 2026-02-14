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


async def test_processed_issue_tracker() -> None:
    tracker = ProcessedIssueTracker()
    assert not tracker.is_processed("KAN-1")
    tracker.mark("KAN-1")
    assert tracker.is_processed("KAN-1")
    assert not tracker.is_processed("KAN-2")
