"""Tests for factory functions in redis_state.py (no Redis — Memory fallbacks only)."""

from __future__ import annotations

from gitlab_copilot_agent.concurrency import (
    MemoryDedup,
    MemoryLock,
    MemoryResultStore,
    MemoryTaskQueue,
)
from gitlab_copilot_agent.redis_state import (
    create_dedup,
    create_lock,
    create_result_store,
    create_task_queue,
)


def test_create_lock_returns_memory_lock() -> None:
    assert isinstance(create_lock(), MemoryLock)


def test_create_dedup_returns_memory_dedup() -> None:
    assert isinstance(create_dedup(), MemoryDedup)


def test_create_result_store_returns_memory_by_default() -> None:
    store = create_result_store()
    assert isinstance(store, MemoryResultStore)


def test_create_task_queue_returns_memory_by_default() -> None:
    queue = create_task_queue()
    assert isinstance(queue, MemoryTaskQueue)
