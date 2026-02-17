"""Tests for state factory functions."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from gitlab_copilot_agent.concurrency import (
    DeduplicationStore,
    DistributedLock,
    MemoryDedup,
    MemoryLock,
)
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.state_factory import create_dedup, create_lock

# Test data constants
MOCK_GITLAB_URL = "https://gitlab.example.com"
MOCK_GITLAB_TOKEN = "glpat-test-token"  # nosec: test token
MOCK_WEBHOOK_SECRET = "webhook-secret-test"  # nosec: test secret
MOCK_GITHUB_TOKEN = "ghp_test_token"  # nosec: test token
MOCK_REDIS_URL = "redis://localhost:6379/0"


@pytest.fixture
def base_settings_dict() -> dict[str, str]:
    """Provide minimal valid settings for testing."""
    return {
        "gitlab_url": MOCK_GITLAB_URL,
        "gitlab_token": MOCK_GITLAB_TOKEN,
        "gitlab_webhook_secret": MOCK_WEBHOOK_SECRET,
        "github_token": MOCK_GITHUB_TOKEN,
    }


async def test_create_lock_memory_backend(base_settings_dict: dict[str, str]) -> None:
    """Factory returns MemoryLock when STATE_BACKEND=memory."""
    settings = Settings(**base_settings_dict)
    lock = await create_lock(settings)

    assert isinstance(lock, MemoryLock)
    assert isinstance(lock, DistributedLock)


async def test_create_lock_redis_backend(
    base_settings_dict: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factory returns RedisLock when STATE_BACKEND=redis."""
    from redis import asyncio as redis_asyncio

    from gitlab_copilot_agent import state_redis

    # Mock Redis.from_url to return fake client
    fake_redis = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(
        redis_asyncio, "Redis", type("Redis", (), {"from_url": lambda url: fake_redis})
    )

    settings = Settings(**base_settings_dict, state_backend="redis", redis_url=MOCK_REDIS_URL)
    lock = await create_lock(settings)

    assert isinstance(lock, state_redis.RedisLock)
    assert isinstance(lock, DistributedLock)


async def test_create_dedup_memory_backend(base_settings_dict: dict[str, str]) -> None:
    """Factory returns MemoryDedup when STATE_BACKEND=memory."""
    settings = Settings(**base_settings_dict)
    dedup = await create_dedup(settings)

    assert isinstance(dedup, MemoryDedup)
    assert isinstance(dedup, DeduplicationStore)


async def test_create_dedup_redis_backend(
    base_settings_dict: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factory returns RedisDedup when STATE_BACKEND=redis."""
    from redis import asyncio as redis_asyncio

    from gitlab_copilot_agent import state_redis

    # Mock Redis.from_url to return fake client
    fake_redis = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(
        redis_asyncio, "Redis", type("Redis", (), {"from_url": lambda url: fake_redis})
    )

    settings = Settings(**base_settings_dict, state_backend="redis", redis_url=MOCK_REDIS_URL)
    dedup = await create_dedup(settings)

    assert isinstance(dedup, state_redis.RedisDedup)
    assert isinstance(dedup, DeduplicationStore)


def test_config_validation_redis_without_url(base_settings_dict: dict[str, str]) -> None:
    """Config validation fails when STATE_BACKEND=redis but REDIS_URL is not set."""
    with pytest.raises(ValueError, match="REDIS_URL is required when STATE_BACKEND=redis"):
        Settings(**base_settings_dict, state_backend="redis")


async def test_factory_raises_without_redis_url(base_settings_dict: dict[str, str]) -> None:
    """Factory functions raise if STATE_BACKEND=redis but REDIS_URL is None.

    (shouldn't happen with validation)
    """
    # Create settings without validation (bypass by setting redis_url then clearing it)
    settings = Settings(**base_settings_dict, state_backend="redis", redis_url=MOCK_REDIS_URL)
    settings.redis_url = None  # Bypass validation

    with pytest.raises(ValueError, match="REDIS_URL is required"):
        await create_lock(settings)

    with pytest.raises(ValueError, match="REDIS_URL is required"):
        await create_dedup(settings)
