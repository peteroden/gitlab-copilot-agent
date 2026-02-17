"""Redis-backed distributed lock and deduplication store.

Uses redis.asyncio for async Redis operations.
RedisLock implements single-instance Redlock algorithm.
RedisDedup uses SET with TTL for deduplication.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import structlog
from redis.asyncio import Redis

log = structlog.get_logger()

_LOCK_PREFIX = "lock:"
_DEDUP_PREFIX = "dedup:"


class RedisLock:
    """Distributed lock using Redis (single-instance Redlock).

    Implements DistributedLock protocol.
    Uses SET NX EX pattern for atomic lock acquisition with TTL.
    Lua script ensures atomic release only if we still own the lock.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    @asynccontextmanager
    async def acquire(self, key: str, ttl_seconds: int = 300) -> AsyncIterator[None]:
        """Acquire lock using SET NX EX pattern with spin-wait."""
        lock_key = f"{_LOCK_PREFIX}{key}"
        lock_value = str(uuid.uuid4())

        # Spin until lock acquired (exponential backoff)
        delay = 0.01
        max_delay = 1.0
        while True:
            acquired = await self._client.set(lock_key, lock_value, nx=True, ex=ttl_seconds)
            if acquired:
                log.debug("lock_acquired", key=key, ttl_seconds=ttl_seconds)
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

        try:
            yield
        finally:
            # Release only if we still own it (Lua script for atomicity)
            release_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            # mypy has issues with redis.eval return type (Awaitable[str] | str)
            released = await self._client.eval(release_script, 1, lock_key, lock_value)  # type: ignore[misc]
            log.debug("lock_released", key=key, released=bool(released))


class RedisDedup:
    """Deduplication store using Redis SET with TTL.

    Implements DeduplicationStore protocol.
    Each key is a Redis string with expiration.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def is_seen(self, key: str) -> bool:
        """Check if key exists in Redis."""
        exists = bool(await self._client.exists(f"{_DEDUP_PREFIX}{key}"))
        log.debug("dedup_check", key=key, exists=exists)
        return exists

    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None:
        """Set key with TTL."""
        await self._client.set(f"{_DEDUP_PREFIX}{key}", "1", ex=ttl_seconds)
        log.debug("dedup_mark", key=key, ttl_seconds=ttl_seconds)
