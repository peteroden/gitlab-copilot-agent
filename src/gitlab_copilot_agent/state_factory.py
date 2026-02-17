"""Factory for creating state backends based on configuration."""

from __future__ import annotations

from typing import cast

from gitlab_copilot_agent.concurrency import (
    DeduplicationStore,
    DistributedLock,
    MemoryDedup,
    MemoryLock,
)
from gitlab_copilot_agent.config import Settings


async def create_lock(settings: Settings) -> DistributedLock:
    """Create distributed lock backend based on STATE_BACKEND config.

    Returns:
        RedisLock if STATE_BACKEND=redis, MemoryLock otherwise.
    """
    if settings.state_backend == "redis":
        from redis.asyncio import Redis

        from gitlab_copilot_agent.state_redis import RedisLock

        if not settings.redis_url:
            raise ValueError("REDIS_URL is required when STATE_BACKEND=redis")

        client = Redis.from_url(settings.redis_url)
        return cast(DistributedLock, RedisLock(client))

    return cast(DistributedLock, MemoryLock())


async def create_dedup(settings: Settings) -> DeduplicationStore:
    """Create deduplication store backend based on STATE_BACKEND config.

    Returns:
        RedisDedup if STATE_BACKEND=redis, MemoryDedup otherwise.
    """
    if settings.state_backend == "redis":
        from redis.asyncio import Redis

        from gitlab_copilot_agent.state_redis import RedisDedup

        if not settings.redis_url:
            raise ValueError("REDIS_URL is required when STATE_BACKEND=redis")

        client = Redis.from_url(settings.redis_url)
        return cast(DeduplicationStore, RedisDedup(client))

    return cast(DeduplicationStore, MemoryDedup())
