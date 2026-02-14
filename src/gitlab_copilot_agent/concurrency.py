"""Per-repo locking and Jira issue deduplication."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RepoLockManager:
    """Async lock per repo URL — serializes operations on the same repo."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def acquire(self, repo_url: str) -> AsyncIterator[None]:
        if repo_url not in self._locks:
            self._locks[repo_url] = asyncio.Lock()
        async with self._locks[repo_url]:
            yield


class ProcessedIssueTracker:
    """Track processed Jira issue keys to avoid re-processing within a run."""

    def __init__(self) -> None:
        self._processed: set[str] = set()

    def is_processed(self, key: str) -> bool:
        return key in self._processed

    def mark(self, key: str) -> None:
        self._processed.add(key)
