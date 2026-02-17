"""Tests for Redis-backed state implementations and factory functions."""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from gitlab_copilot_agent.concurrency import (
    DeduplicationStore,
    DistributedLock,
    MemoryDedup,
    MemoryLock,
)
from gitlab_copilot_agent.redis_state import (
    _DEDUP_PREFIX,
    _LOCK_PREFIX,
    RedisDedup,
    RedisLock,
    create_dedup,
    create_lock,
)
from tests.conftest import make_settings

# -- Test constants --
LOCK_KEY = "repo:group/project"
DEDUP_KEY = "issue:KAN-42"
REDIS_URL = "redis://localhost:6379/0"
LOCK_TTL = 10
DEDUP_TTL = 60


@pytest.fixture()
def fake_redis() -> fakeredis.FakeAsyncRedis:
    """Isolated fake Redis client per test."""
    return fakeredis.FakeAsyncRedis()


# -- Protocol conformance --


async def test_redis_lock_implements_protocol(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    assert isinstance(RedisLock(fake_redis), DistributedLock)


async def test_redis_dedup_implements_protocol(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    assert isinstance(RedisDedup(fake_redis), DeduplicationStore)


# -- RedisLock tests --


async def test_lock_acquire_and_release(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    lock = RedisLock(fake_redis)
    async with lock.acquire(LOCK_KEY, ttl_seconds=LOCK_TTL):
        assert await fake_redis.exists(f"{_LOCK_PREFIX}{LOCK_KEY}")
    # After release, key is gone
    assert not await fake_redis.exists(f"{_LOCK_PREFIX}{LOCK_KEY}")


async def test_lock_exclusivity(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    """Two concurrent tasks on same key never overlap."""
    lock = RedisLock(fake_redis)
    held = False
    violations = 0

    async def task() -> None:
        nonlocal held, violations
        async with lock.acquire(LOCK_KEY, ttl_seconds=LOCK_TTL):
            if held:
                violations += 1
            held = True
            await asyncio.sleep(0.05)
            held = False

    await asyncio.gather(task(), task())
    assert violations == 0


async def test_lock_different_keys_parallel(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    """Different keys can be held concurrently."""
    lock = RedisLock(fake_redis)
    held: list[str] = []

    async def task(key: str) -> None:
        async with lock.acquire(key, ttl_seconds=LOCK_TTL):
            held.append(key)
            await asyncio.sleep(0.05)

    await asyncio.gather(task("repo-a"), task("repo-b"))
    assert len(held) == 2


async def test_lock_renewal_extends_ttl(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    """Lock TTL is renewed while held, preventing expiration during long work."""
    lock = RedisLock(fake_redis)
    short_ttl = 2  # 2 seconds; renewal at 1s interval

    async with lock.acquire(LOCK_KEY, ttl_seconds=short_ttl):
        # Wait longer than half the TTL to trigger renewal
        await asyncio.sleep(1.5)
        # Lock should still exist because renewal extended it
        assert await fake_redis.exists(f"{_LOCK_PREFIX}{LOCK_KEY}")
        ttl: int = await fake_redis.ttl(f"{_LOCK_PREFIX}{LOCK_KEY}")
        assert ttl > 0


# -- RedisDedup tests --


async def test_dedup_unseen_returns_false(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    store = RedisDedup(fake_redis)
    assert not await store.is_seen(DEDUP_KEY)


async def test_dedup_mark_then_seen(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    store = RedisDedup(fake_redis)
    await store.mark_seen(DEDUP_KEY, ttl_seconds=DEDUP_TTL)
    assert await store.is_seen(DEDUP_KEY)


async def test_dedup_ttl_is_set(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    store = RedisDedup(fake_redis)
    await store.mark_seen(DEDUP_KEY, ttl_seconds=DEDUP_TTL)
    ttl: int = await fake_redis.ttl(f"{_DEDUP_PREFIX}{DEDUP_KEY}")
    assert 0 < ttl <= DEDUP_TTL


async def test_dedup_different_keys_independent(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    store = RedisDedup(fake_redis)
    await store.mark_seen("key-a", ttl_seconds=DEDUP_TTL)
    assert await store.is_seen("key-a")
    assert not await store.is_seen("key-b")


# -- aclose() lifecycle tests --


async def test_redis_lock_aclose(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    """RedisLock.aclose() delegates to the Redis client without error."""
    lock = RedisLock(fake_redis)
    await lock.aclose()


async def test_redis_dedup_aclose(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    """RedisDedup.aclose() delegates to the Redis client without error."""
    store = RedisDedup(fake_redis)
    await store.aclose()


async def test_memory_lock_aclose() -> None:
    """MemoryLock.aclose() is a no-op and does not raise."""
    lock = MemoryLock()
    await lock.aclose()
    # Still usable after aclose — in-memory has no connection to close
    async with lock.acquire("key"):
        pass


async def test_memory_dedup_aclose() -> None:
    """MemoryDedup.aclose() is a no-op and does not raise."""
    store = MemoryDedup()
    await store.aclose()
    assert not await store.is_seen("key")


# -- Factory tests --


def test_create_lock_memory_backend() -> None:
    lock = create_lock("memory")
    assert isinstance(lock, MemoryLock)


def test_create_dedup_memory_backend() -> None:
    dedup = create_dedup("memory")
    assert isinstance(dedup, MemoryDedup)


def test_create_lock_redis_missing_url() -> None:
    with pytest.raises(ValueError, match="redis_url"):
        create_lock("redis")


def test_create_dedup_redis_missing_url() -> None:
    with pytest.raises(ValueError, match="redis_url"):
        create_dedup("redis")


def test_create_lock_redis_backend() -> None:
    lock = create_lock("redis", redis_url=REDIS_URL)
    assert isinstance(lock, RedisLock)


def test_create_dedup_redis_backend() -> None:
    dedup = create_dedup("redis", redis_url=REDIS_URL)
    assert isinstance(dedup, RedisDedup)


# -- Config validation tests --


def test_config_redis_requires_url() -> None:
    """STATE_BACKEND=redis without REDIS_URL raises ValidationError."""
    with pytest.raises(Exception, match="REDIS_URL"):
        make_settings(state_backend="redis")


def test_config_redis_with_url() -> None:
    settings = make_settings(state_backend="redis", redis_url=REDIS_URL)
    assert settings.state_backend == "redis"
    assert settings.redis_url == REDIS_URL


def test_config_defaults_to_memory() -> None:
    settings = make_settings()
    assert settings.state_backend == "memory"
    assert settings.redis_url is None
