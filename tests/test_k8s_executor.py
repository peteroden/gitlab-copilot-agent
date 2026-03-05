"""Tests for KubernetesTaskExecutor."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from gitlab_copilot_agent.k8s_executor import _parse_result
from gitlab_copilot_agent.task_executor import (
    CodingResult,
    ReviewResult,
    TaskExecutor,
    TaskParams,
)
from tests.conftest import make_settings

# -- Test constants -------------------------------------------------------

TASK_ID = "abc12345-6789-0def-ghij-klmnopqrstuv"
TASK_TYPE = "review"
REPO_URL = "https://gitlab.example.com/group/project.git"
BRANCH = "feature/x"
SYSTEM_PROMPT = "You are a reviewer."
USER_PROMPT = "Review this code."
K8S_NAMESPACE = "ci"
K8S_JOB_IMAGE = "registry.example.com/agent:latest"
K8S_JOB_CPU = "500m"
K8S_JOB_MEM = "512Mi"
K8S_JOB_TIMEOUT = 5  # short for tests
K8S_JOB_TIMEOUT_SHORT = 1  # for timeout tests
CACHED_RESULT = "cached review output"
FRESH_RESULT = "fresh review output"
LOCK_KEY = f"k8s_exec:{TASK_ID}"
FAST_POLL_INTERVAL = 0.05  # speed up polling in tests
CODING_JSON_RESULT = json.dumps({"result_type": "coding", "summary": "coded it"})
REVIEW_JSON_RESULT = json.dumps({"result_type": "review", "summary": "looks good"})
PLAIN_TEXT_RESULT = "plain text output"
INVALID_JSON = "{not-valid-json"
MALFORMED_LOCK = "garbage"


# -- Helpers / fixtures ---------------------------------------------------


def _make_settings(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "task_executor": "kubernetes",
        "k8s_namespace": K8S_NAMESPACE,
        "k8s_job_image": K8S_JOB_IMAGE,
        "k8s_job_cpu_limit": K8S_JOB_CPU,
        "k8s_job_memory_limit": K8S_JOB_MEM,
        "k8s_job_timeout": K8S_JOB_TIMEOUT,
    }
    return make_settings(**(defaults | overrides))


def _make_task(**overrides: Any) -> TaskParams:
    defaults: dict[str, Any] = {
        "task_type": TASK_TYPE,
        "task_id": TASK_ID,
        "repo_url": REPO_URL,
        "branch": BRANCH,
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": USER_PROMPT,
        "settings": _make_settings(),
    }
    return TaskParams(**(defaults | overrides))


@pytest.fixture
def fake_result_store() -> Any:
    from gitlab_copilot_agent.concurrency import MemoryResultStore

    return MemoryResultStore()


def _make_executor(
    settings: Any | None = None,
    result_store: Any | None = None,
) -> Any:
    from gitlab_copilot_agent.concurrency import MemoryResultStore, MemoryTaskQueue
    from gitlab_copilot_agent.k8s_executor import KubernetesTaskExecutor

    if result_store is None:
        result_store = MemoryResultStore()
    task_queue = MemoryTaskQueue()
    return KubernetesTaskExecutor(
        settings=settings or _make_settings(),
        result_store=result_store,
        task_queue=task_queue,
    )


# -- Tests ----------------------------------------------------------------


class TestProtocolCompliance:
    def test_implements_task_executor(self) -> None:
        executor = _make_executor()
        assert isinstance(executor, TaskExecutor)


class TestIdempotency:
    async def test_returns_cached_result(self, fake_result_store: Any) -> None:
        await fake_result_store.set(TASK_ID, CACHED_RESULT)
        executor = _make_executor(result_store=fake_result_store)

        result = await executor.execute(_make_task())

        assert result.summary == CACHED_RESULT


class TestExecuteViaQueue:
    """Fresh enqueue path — no cached result, no lock."""

    async def test_enqueues_and_returns_result(
        self, fake_result_store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gitlab_copilot_agent.k8s_executor as mod

        monkeypatch.setattr(mod, "_JOB_POLL_INTERVAL", FAST_POLL_INTERVAL)

        executor = _make_executor(result_store=fake_result_store)

        async def _write_result_later() -> None:
            await asyncio.sleep(0.1)
            await fake_result_store.set(TASK_ID, FRESH_RESULT)

        asyncio.create_task(_write_result_later())
        result = await executor.execute(_make_task())

        assert result.summary == FRESH_RESULT
        assert isinstance(result, ReviewResult)


class TestExecuteViaQueueIdempotency:
    """Lock-skip path: task already enqueued, result arrives via poll."""

    async def test_skips_enqueue_when_lock_active(
        self, fake_result_store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gitlab_copilot_agent.k8s_executor as mod

        monkeypatch.setattr(mod, "_JOB_POLL_INTERVAL", FAST_POLL_INTERVAL)

        future_ts = int(time.time()) + K8S_JOB_TIMEOUT
        await fake_result_store.set(LOCK_KEY, f"enqueued:{future_ts}")

        async def _write_result_later() -> None:
            await asyncio.sleep(0.1)
            await fake_result_store.set(TASK_ID, FRESH_RESULT)

        executor = _make_executor(result_store=fake_result_store)
        asyncio.create_task(_write_result_later())
        result = await executor.execute(_make_task())

        assert result.summary == FRESH_RESULT
        assert isinstance(result, ReviewResult)


class TestExecuteViaQueueExpiredLock:
    """Lock exists but expired — executor should re-enqueue."""

    async def test_re_enqueues_when_lock_expired(
        self, fake_result_store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gitlab_copilot_agent.k8s_executor as mod

        monkeypatch.setattr(mod, "_JOB_POLL_INTERVAL", FAST_POLL_INTERVAL)

        expired_ts = int(time.time()) - 10
        await fake_result_store.set(LOCK_KEY, f"enqueued:{expired_ts}")

        async def _write_result_later() -> None:
            await asyncio.sleep(0.1)
            await fake_result_store.set(TASK_ID, FRESH_RESULT)

        executor = _make_executor(result_store=fake_result_store)
        asyncio.create_task(_write_result_later())
        result = await executor.execute(_make_task())

        assert result.summary == FRESH_RESULT


class TestExecuteViaQueueMalformedLock:
    """Lock value is malformed — executor falls through to enqueue."""

    async def test_enqueues_when_lock_malformed(
        self, fake_result_store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gitlab_copilot_agent.k8s_executor as mod

        monkeypatch.setattr(mod, "_JOB_POLL_INTERVAL", FAST_POLL_INTERVAL)

        await fake_result_store.set(LOCK_KEY, MALFORMED_LOCK)

        async def _write_result_later() -> None:
            await asyncio.sleep(0.1)
            await fake_result_store.set(TASK_ID, FRESH_RESULT)

        executor = _make_executor(result_store=fake_result_store)
        asyncio.create_task(_write_result_later())
        result = await executor.execute(_make_task())

        assert result.summary == FRESH_RESULT


class TestPollTimeout:
    """_poll_result raises TimeoutError when no result appears."""

    async def test_raises_timeout_error(
        self, fake_result_store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gitlab_copilot_agent.k8s_executor as mod

        monkeypatch.setattr(mod, "_JOB_POLL_INTERVAL", FAST_POLL_INTERVAL)

        settings = _make_settings(k8s_job_timeout=K8S_JOB_TIMEOUT_SHORT)
        executor = _make_executor(settings=settings, result_store=fake_result_store)

        with pytest.raises(TimeoutError, match=TASK_ID):
            await executor.execute(_make_task())


class TestParseResult:
    """Unit tests for _parse_result function."""

    def test_json_coding_result(self) -> None:
        result = _parse_result(CODING_JSON_RESULT, "review")
        assert isinstance(result, CodingResult)
        assert result.summary == "coded it"

    def test_json_review_result(self) -> None:
        result = _parse_result(REVIEW_JSON_RESULT, "coding")
        assert isinstance(result, ReviewResult)
        assert result.summary == "looks good"

    def test_plain_text_coding_fallback(self) -> None:
        result = _parse_result(PLAIN_TEXT_RESULT, "coding")
        assert isinstance(result, CodingResult)
        assert result.summary == PLAIN_TEXT_RESULT

    def test_plain_text_review_fallback(self) -> None:
        result = _parse_result(PLAIN_TEXT_RESULT, "review")
        assert isinstance(result, ReviewResult)
        assert result.summary == PLAIN_TEXT_RESULT

    def test_invalid_json_falls_back_to_plain(self) -> None:
        result = _parse_result(INVALID_JSON, "review")
        assert isinstance(result, ReviewResult)
        assert result.summary == INVALID_JSON

    def test_json_without_result_type_falls_back(self) -> None:
        raw = json.dumps({"foo": "bar"})
        result = _parse_result(raw, "coding")
        assert isinstance(result, CodingResult)
        assert result.summary == raw
