"""Tests for RemoteTaskExecutor — unified remote dispatch."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.concurrency import MemoryResultStore, MemoryTaskQueue
from gitlab_copilot_agent.remote_executor import RemoteTaskExecutor, parse_result
from gitlab_copilot_agent.task_executor import (
    CodingResult,
    ReviewResult,
    TaskExecutionError,
    TaskExecutor,
    TaskParams,
)
from tests.conftest import make_settings

# -- Constants --

TASK_ID = "remote-test-task-001"
TASK_TYPE = "review"
REPO_URL = "https://gitlab.example.com/group/project.git"
BRANCH = "feature/x"
SYSTEM_PROMPT = "You are a reviewer."
USER_PROMPT = "Review this code."
JOB_TIMEOUT = 3
CACHED_RESULT = "cached review output"
LOCK_KEY = f"remote_exec:{TASK_ID}"


def _make_task(**overrides: Any) -> TaskParams:
    defaults: dict[str, Any] = {
        "task_type": TASK_TYPE,
        "task_id": TASK_ID,
        "repo_url": REPO_URL,
        "branch": BRANCH,
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": USER_PROMPT,
        "settings": make_settings(),
    }
    return TaskParams(**(defaults | overrides))


def _make_executor(
    store: MemoryResultStore | None = None,
    queue: MemoryTaskQueue | None = None,
    timeout: int = JOB_TIMEOUT,
) -> RemoteTaskExecutor:
    return RemoteTaskExecutor(
        result_store=store or MemoryResultStore(),
        task_queue=queue or MemoryTaskQueue(),
        job_timeout=timeout,
    )


# -- Protocol compliance --


class TestProtocolCompliance:
    def test_implements_task_executor(self) -> None:
        executor = _make_executor()
        assert isinstance(executor, TaskExecutor)


# -- parse_result --


class TestParseResult:
    def test_review_json(self) -> None:
        raw = json.dumps({"result_type": "review", "summary": "looks good"})
        result = parse_result(raw, "review")
        assert isinstance(result, ReviewResult)
        assert result.summary == "looks good"

    def test_coding_json(self) -> None:
        raw = json.dumps({"result_type": "coding", "summary": "coded it"})
        result = parse_result(raw, "coding")
        assert isinstance(result, CodingResult)

    def test_error_json_raises(self) -> None:
        raw = json.dumps(
            {
                "result_type": "error",
                "summary": "Task failed",
                "traceback": "Traceback...",
            }
        )
        with pytest.raises(TaskExecutionError, match="Task failed"):
            parse_result(raw, "review")

    def test_raw_string_review(self) -> None:
        result = parse_result("plain text output", "review")
        assert isinstance(result, ReviewResult)
        assert result.summary == "plain text output"

    def test_raw_string_coding(self) -> None:
        result = parse_result("plain coding output", "coding")
        assert isinstance(result, CodingResult)
        assert result.summary == "plain coding output"

    def test_invalid_json_falls_back(self) -> None:
        result = parse_result("{invalid", "review")
        assert isinstance(result, ReviewResult)


# -- Cached result --


class TestCachedResult:
    async def test_returns_cached_without_enqueuing(self) -> None:
        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        await store.set(TASK_ID, json.dumps({"result_type": "review", "summary": CACHED_RESULT}))

        executor = _make_executor(store=store, queue=queue)
        result = await executor.execute(_make_task())

        assert isinstance(result, ReviewResult)
        assert result.summary == CACHED_RESULT


# -- Idempotent enqueue --


class TestIdempotentEnqueue:
    async def test_skips_enqueue_when_lock_exists(self) -> None:
        """If a valid lock exists, executor polls without re-enqueuing."""
        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        import time

        await store.set(LOCK_KEY, f"enqueued:{int(time.time()) + 300}")
        # Pre-stage result so poll returns immediately
        await store.set(TASK_ID, json.dumps({"result_type": "review", "summary": "done"}))

        executor = _make_executor(store=store, queue=queue)
        result = await executor.execute(_make_task())
        assert isinstance(result, ReviewResult)
        assert result.summary == "done"


# -- Poll + enqueue --


class TestEnqueueAndPoll:
    async def test_enqueues_and_polls_result(self) -> None:
        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        executor = _make_executor(store=store, queue=queue)

        async def _delayed_result() -> None:
            await asyncio.sleep(0.1)
            await store.set(TASK_ID, json.dumps({"result_type": "review", "summary": "fresh"}))

        with patch("gitlab_copilot_agent.remote_executor._POLL_INTERVAL", 0.05):
            asyncio.get_event_loop().create_task(_delayed_result())
            result = await executor.execute(_make_task(repo_path=None))

        assert isinstance(result, ReviewResult)
        assert result.summary == "fresh"
        lock = await store.get(LOCK_KEY)
        assert lock is not None and lock.startswith("enqueued:")

    async def test_uploads_repo_tarball(self) -> None:
        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        executor = _make_executor(store=store, queue=queue)

        # Pre-stage result so poll returns immediately
        async def _stage_result() -> None:
            await asyncio.sleep(0.05)
            await store.set(TASK_ID, json.dumps({"result_type": "review", "summary": "ok"}))

        with (
            patch(
                "gitlab_copilot_agent.remote_executor.tar_repo_to_bytes",
                new_callable=AsyncMock,
                return_value=b"fake-tarball",
            ),
            patch("gitlab_copilot_agent.remote_executor._POLL_INTERVAL", 0.02),
        ):
            asyncio.get_event_loop().create_task(_stage_result())
            result = await executor.execute(_make_task(repo_path="/tmp/fake-repo"))

        assert isinstance(result, ReviewResult)


# -- Timeout --


class TestTimeout:
    async def test_raises_on_timeout(self) -> None:
        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        executor = _make_executor(store=store, queue=queue, timeout=1)

        with (
            patch("gitlab_copilot_agent.remote_executor._POLL_INTERVAL", 0.05),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            await executor.execute(_make_task(repo_path=None))


# -- Dispatch payload security --


class TestDispatchPayloadSecurity:
    async def test_payload_excludes_secrets(self) -> None:
        """Dispatch payload must not contain tokens or API keys."""
        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        executor = _make_executor(store=store, queue=queue)

        # Stage result after enqueue so poll returns quickly
        async def _stage_result() -> None:
            await asyncio.sleep(0.05)
            await store.set(TASK_ID, json.dumps({"result_type": "review", "summary": "ok"}))

        with patch("gitlab_copilot_agent.remote_executor._POLL_INTERVAL", 0.02):
            asyncio.get_event_loop().create_task(_stage_result())
            await executor.execute(_make_task(repo_path=None))

        # Check enqueued payload
        msg = await queue.dequeue()
        assert msg is not None
        payload = json.loads(msg.payload)
        keys = set(payload.keys())
        expected = {
            "task_type",
            "task_id",
            "repo_blob_key",
            "system_prompt",
            "user_prompt",
            "plugins",
        }
        assert keys == expected
        secret_keys = {"gitlab_token", "github_token", "copilot_provider_api_key"}
        assert keys.isdisjoint(secret_keys)
