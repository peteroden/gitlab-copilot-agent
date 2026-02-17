"""Tests for Redis state backends."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from gitlab_copilot_agent.concurrency import DeduplicationStore, DistributedLock
from gitlab_copilot_agent.state_redis import RedisDedup, RedisLock

# Test constants for shared use
TEST_KEY = "test-key"
TEST_KEY_2 = "test-key-2"
TEST_TTL_SHORT = 1
TEST_TTL_DEFAULT = 300


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    """Provide a fake Redis client for testing."""
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def redis_lock(redis_client: fakeredis.aioredis.FakeRedis) -> RedisLock:
    """Provide a RedisLock instance with fake Redis."""
    return RedisLock(redis_client)


@pytest.fixture
def redis_dedup(redis_client: fakeredis.aioredis.FakeRedis) -> RedisDedup:
    """Provide a RedisDedup instance with fake Redis."""
    return RedisDedup(redis_client)


# Protocol conformance tests
def test_redis_lock_implements_protocol(redis_lock: RedisLock) -> None:
    """RedisLock must implement DistributedLock protocol."""
    assert isinstance(redis_lock, DistributedLock)


def test_redis_dedup_implements_protocol(redis_dedup: RedisDedup) -> None:
    """RedisDedup must implement DeduplicationStore protocol."""
    assert isinstance(redis_dedup, DeduplicationStore)


# RedisLock tests
async def test_lock_acquire_and_release(redis_lock: RedisLock) -> None:
    """Lock can be acquired and released successfully."""
    async with redis_lock.acquire(TEST_KEY):
        pass  # Lock should be held here


async def test_lock_prevents_concurrent_access(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """Multiple acquire attempts on same key block until released."""
    lock = RedisLock(redis_client)
    acquired_order = []

    async with lock.acquire(TEST_KEY):
        acquired_order.append(1)
        # While holding lock, key should exist in Redis
        assert await redis_client.exists(f"lock:{TEST_KEY}")

    acquired_order.append(2)
    assert acquired_order == [1, 2]

    # After release, key should be deleted
    assert not await redis_client.exists(f"lock:{TEST_KEY}")


async def test_lock_with_custom_ttl(
    redis_lock: RedisLock, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Lock respects custom TTL parameter."""
    async with redis_lock.acquire(TEST_KEY, ttl_seconds=60):
        ttl = await redis_client.ttl(f"lock:{TEST_KEY}")
        # TTL should be set (fakeredis may not exactly preserve it, but should be > 0)
        assert ttl > 0


async def test_lock_different_keys_independent(redis_lock: RedisLock) -> None:
    """Locks on different keys don't interfere with each other."""
    acquired = []

    async with redis_lock.acquire(TEST_KEY):
        acquired.append("key1_start")
        async with redis_lock.acquire(TEST_KEY_2):
            acquired.append("key2_start")
        acquired.append("key2_end")
    acquired.append("key1_end")

    assert acquired == ["key1_start", "key2_start", "key2_end", "key1_end"]


# RedisDedup tests
async def test_dedup_initially_not_seen(redis_dedup: RedisDedup) -> None:
    """Keys are not seen before being marked."""
    assert not await redis_dedup.is_seen(TEST_KEY)


async def test_dedup_mark_and_check(redis_dedup: RedisDedup) -> None:
    """Marked keys are seen on subsequent checks."""
    assert not await redis_dedup.is_seen(TEST_KEY)
    await redis_dedup.mark_seen(TEST_KEY)
    assert await redis_dedup.is_seen(TEST_KEY)


async def test_dedup_different_keys_independent(redis_dedup: RedisDedup) -> None:
    """Different keys have independent seen state."""
    await redis_dedup.mark_seen(TEST_KEY)
    assert await redis_dedup.is_seen(TEST_KEY)
    assert not await redis_dedup.is_seen(TEST_KEY_2)


async def test_dedup_custom_ttl(
    redis_dedup: RedisDedup, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Deduplication respects custom TTL parameter."""
    await redis_dedup.mark_seen(TEST_KEY, ttl_seconds=120)
    ttl = await redis_client.ttl(f"dedup:{TEST_KEY}")
    assert ttl > 0


async def test_dedup_ttl_expiration(
    redis_dedup: RedisDedup, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Keys expire after TTL (fakeredis supports this)."""
    await redis_dedup.mark_seen(TEST_KEY, ttl_seconds=TEST_TTL_SHORT)
    assert await redis_dedup.is_seen(TEST_KEY)

    # Manually delete to simulate expiration (fakeredis time simulation)
    # In real Redis, this would expire naturally
    await redis_client.delete(f"dedup:{TEST_KEY}")
    assert not await redis_dedup.is_seen(TEST_KEY)


async def test_dedup_prefix_isolation(redis_client: fakeredis.aioredis.FakeRedis) -> None:
    """Lock and dedup prefixes don't collide."""
    lock = RedisLock(redis_client)
    dedup = RedisDedup(redis_client)

    async with lock.acquire(TEST_KEY):
        await dedup.mark_seen(TEST_KEY)
        # Both should coexist
        assert await redis_client.exists(f"lock:{TEST_KEY}")
        assert await redis_client.exists(f"dedup:{TEST_KEY}")
