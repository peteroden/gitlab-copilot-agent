"""Redis-backed implementations for Lock and DeduplicationStore."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from gitlab_copilot_agent.concurrency import (
    DeduplicationStore,
    DistributedLock,
    MemoryDedup,
    MemoryLock,
    MemoryResultStore,
    ResultStore,
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

    def __init__(self, client: Redis) -> None:
        self._client: Redis = client

    @asynccontextmanager
    async def acquire(self, key: str, ttl_seconds: int = 300) -> AsyncIterator[None]:
        lock_key = f"{_LOCK_PREFIX}{key}"
        token = uuid4().hex
        while not await self._client.set(lock_key, token, nx=True, ex=ttl_seconds):
            await asyncio.sleep(_LOCK_RETRY_DELAY)

        renewal_task = asyncio.create_task(self._renew_loop(lock_key, token, ttl_seconds))
        try:
            yield
        finally:
            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task
            with suppress(RedisConnectionError, RedisTimeoutError, OSError):
                await self._client.eval(  # type: ignore[misc]
                    _UNLOCK_SCRIPT, 1, lock_key, token
                )

    async def _renew_loop(self, lock_key: str, token: str, ttl_seconds: int) -> None:
        """Periodically extend the lock TTL while it is held."""
        interval = max(1, int(ttl_seconds * _RENEWAL_FACTOR))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._client.eval(  # type: ignore[misc]
                    _EXTEND_SCRIPT, 1, lock_key, token, str(ttl_seconds)
                )
            except (RedisConnectionError, RedisTimeoutError, OSError):
                log.warning("lock_renewal_failed", key=lock_key)
                return

    async def aclose(self) -> None:
        """Close the underlying Redis connection."""
        await self._client.aclose()


class RedisDedup:
    """Redis-backed deduplication store using SET + TTL.

    Connection failures are handled gracefully: ``is_seen`` returns ``False``
    (tolerate occasional duplicates) and ``mark_seen`` is best-effort.
    """

    def __init__(self, client: Redis) -> None:
        self._client: Redis = client

    async def is_seen(self, key: str) -> bool:
        try:
            result: int = await self._client.exists(f"{_DEDUP_PREFIX}{key}")
            return result > 0
        except (RedisConnectionError, RedisTimeoutError, OSError):
            log.warning("redis_dedup_unreachable", op="is_seen", key=key)
            return False

    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None:
        try:
            await self._client.set(f"{_DEDUP_PREFIX}{key}", "1", ex=ttl_seconds)
        except (RedisConnectionError, RedisTimeoutError, OSError):
            log.warning("redis_dedup_unreachable", op="mark_seen", key=key)

    async def aclose(self) -> None:
        """Close the underlying Redis connection."""
        await self._client.aclose()


_RESULT_PREFIX = "result:"


class RedisResultStore:
    """Redis-backed task result store.

    Connection failures are handled gracefully: ``get`` returns ``None``
    and ``set`` is best-effort.
    """

    def __init__(self, client: Redis) -> None:
        self._client: Redis = client

    async def get(self, key: str) -> str | None:
        try:
            val = await self._client.get(f"{_RESULT_PREFIX}{key}")
        except (RedisConnectionError, RedisTimeoutError, OSError):
            log.warning("redis_result_unreachable", op="get", key=key)
            return None
        if val is None:
            return None
        return val.decode() if isinstance(val, bytes) else str(val)

    async def set(self, key: str, value: str, ttl: int = 3600) -> None:
        try:
            await self._client.set(f"{_RESULT_PREFIX}{key}", value, ex=ttl)
        except (RedisConnectionError, RedisTimeoutError, OSError):
            log.warning("redis_result_unreachable", op="set", key=key)

    async def aclose(self) -> None:
        await self._client.aclose()


def _create_redis_client(
    redis_url: str | None = None,
    redis_host: str | None = None,
    redis_port: int = 6380,
    azure_client_id: str | None = None,
) -> Redis:
    """Create a Redis async client using either a URL or Entra ID auth.

    When *redis_host* is provided, authentication uses the ``redis-entraid``
    credential provider with ``DefaultAzureCredential`` (managed identity in
    Azure, CLI locally).  Falls back to ``redis_url`` for non-Azure envs.
    """
    import redis.asyncio as aioredis

    if redis_host:
        try:
            from azure.identity import DefaultAzureCredential
            from redis_entraid.cred_provider import (  # type: ignore[import-untyped]
                create_from_default_azure_credential,
            )
        except ImportError as exc:  # pragma: no cover
            msg = "redis-entraid and azure-identity are required for Entra ID Redis auth"
            raise ImportError(msg) from exc

        kwargs: dict[str, object] = {}
        if azure_client_id:
            kwargs["managed_identity_client_id"] = azure_client_id

        credential_provider = create_from_default_azure_credential(
            ("https://redis.azure.com/.default",),
            credential=DefaultAzureCredential(**kwargs),
        )
        return aioredis.Redis(
            host=redis_host,
            port=redis_port,
            ssl=True,
            credential_provider=credential_provider,
        )

    if not redis_url:
        msg = "Either redis_url or redis_host is required"
        raise ValueError(msg)

    return aioredis.from_url(redis_url)


def create_lock(
    backend: str,
    redis_url: str | None = None,
    redis_host: str | None = None,
    redis_port: int = 6380,
    azure_client_id: str | None = None,
) -> DistributedLock:
    """Factory: create a Lock for the given backend."""
    if backend == "redis":
        return RedisLock(_create_redis_client(redis_url, redis_host, redis_port, azure_client_id))
    return MemoryLock()


def create_dedup(
    backend: str,
    redis_url: str | None = None,
    redis_host: str | None = None,
    redis_port: int = 6380,
    azure_client_id: str | None = None,
) -> DeduplicationStore:
    """Factory: create a DeduplicationStore for the given backend."""
    if backend == "redis":
        return RedisDedup(_create_redis_client(redis_url, redis_host, redis_port, azure_client_id))
    return MemoryDedup()


def create_result_store(
    backend: str,
    redis_url: str | None = None,
    redis_host: str | None = None,
    redis_port: int = 6380,
    azure_client_id: str | None = None,
) -> ResultStore:
    """Factory: create a ResultStore for the given backend."""
    if backend == "redis":
        return RedisResultStore(
            _create_redis_client(redis_url, redis_host, redis_port, azure_client_id)
        )
    return MemoryResultStore()
