"""Redis-backed implementations for DistributedLock and DeduplicationStore."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

from gitlab_copilot_agent.concurrency import (
    DeduplicationStore,
    Lock,
    MemoryDedup,
    MemoryLock,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = structlog.get_logger()

# Lua: atomically release lock only if we still own it
_UNLOCK_SCRIPT = (
    "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
)
# Lua: extend TTL only if we still own the lock
_EXTEND_SCRIPT = (
    "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('expire',KEYS[1],ARGV[2]) "
    "else return 0 end"
)

_LOCK_RETRY_DELAY = 0.1  # seconds between acquisition retries
_LOCK_PREFIX = "lock:"
_DEDUP_PREFIX = "dedup:"
_RENEWAL_FACTOR = 0.5  # renew at half the TTL


class RedisLock:
    """Redis-backed distributed lock using SET NX + TTL (single-instance Redlock).

    Includes automatic TTL renewal to prevent expiration during long
    critical sections.
    """

    def __init__(self, client: Redis[bytes]) -> None:
        self._client = client

    @asynccontextmanager
    async def acquire(self, key: str, ttl_seconds: int = 300) -> AsyncIterator[None]:
        lock_key = f"{_LOCK_PREFIX}{key}"
        token = uuid4().hex
        while not await self._client.set(  # type: ignore[misc]
            lock_key, token, nx=True, ex=ttl_seconds
        ):
            await asyncio.sleep(_LOCK_RETRY_DELAY)

        renewal_task = asyncio.create_task(self._renew_loop(lock_key, token, ttl_seconds))
        try:
            yield
        finally:
            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task
            with suppress(ConnectionError, OSError):
                await self._client.eval(  # type: ignore[union-attr]
                    _UNLOCK_SCRIPT, 1, lock_key, token
                )

    async def _renew_loop(self, lock_key: str, token: str, ttl_seconds: int) -> None:
        """Periodically extend the lock TTL while it is held."""
        interval = max(1, int(ttl_seconds * _RENEWAL_FACTOR))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._client.eval(  # type: ignore[union-attr]
                    _EXTEND_SCRIPT, 1, lock_key, token, str(ttl_seconds)
                )
            except (ConnectionError, OSError):
                log.warning("lock_renewal_failed", key=lock_key)
                return

    async def aclose(self) -> None:
        """Close the underlying Redis connection."""
        await self._client.aclose()  # type: ignore[union-attr]


class RedisDedup:
    """Redis-backed deduplication store using SET + TTL."""

    def __init__(self, client: Redis[bytes]) -> None:
        self._client = client

    async def is_seen(self, key: str) -> bool:
        result: int = await self._client.exists(f"{_DEDUP_PREFIX}{key}")  # type: ignore[assignment]
        return result > 0

    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None:
        await self._client.set(f"{_DEDUP_PREFIX}{key}", "1", ex=ttl_seconds)  # type: ignore[misc]

    async def aclose(self) -> None:
        """Close the underlying Redis connection."""
        await self._client.aclose()  # type: ignore[union-attr]


def create_lock(backend: str, redis_url: str | None = None) -> Lock:
    """Factory: create a Lock for the given backend."""
    if backend == "redis":
        import redis.asyncio as aioredis

        if not redis_url:
            msg = "redis_url is required when backend='redis'"
            raise ValueError(msg)
        return RedisLock(aioredis.from_url(redis_url))
    return MemoryLock()


def create_dedup(backend: str, redis_url: str | None = None) -> DeduplicationStore:
    """Factory: create a DeduplicationStore for the given backend."""
    if backend == "redis":
        import redis.asyncio as aioredis

        if not redis_url:
            msg = "redis_url is required when backend='redis'"
            raise ValueError(msg)
        return RedisDedup(aioredis.from_url(redis_url))
    return MemoryDedup()
