"""Tests for ContainerAppsTaskExecutor."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from gitlab_copilot_agent.concurrency import MemoryResultStore, MemoryTaskQueue
from gitlab_copilot_agent.task_executor import (
    CodingResult,
    ReviewResult,
    TaskExecutor,
    TaskParams,
)
from tests.conftest import make_settings

# -- Test constants -------------------------------------------------------

TASK_ID = "aca-test-task-001"
TASK_TYPE = "review"
REPO_URL = "https://gitlab.example.com/group/project.git"
BRANCH = "feature/aca"
SYSTEM_PROMPT = "You are a reviewer."
USER_PROMPT = "Review this code."
ACA_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
ACA_RESOURCE_GROUP = "rg-copilot-test"
ACA_JOB_NAME = "copilot-job"
ACA_JOB_TIMEOUT = 3  # short for tests
CACHED_RESULT = "cached review output"


# -- Helpers / fixtures ---------------------------------------------------


def _make_settings(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "task_executor": "container_apps",
        "aca_subscription_id": ACA_SUBSCRIPTION_ID,
        "aca_resource_group": ACA_RESOURCE_GROUP,
        "aca_job_name": ACA_JOB_NAME,
        "aca_job_timeout": ACA_JOB_TIMEOUT,
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


# -- Tests ----------------------------------------------------------------


class TestProtocolCompliance:
    def test_implements_task_executor_protocol(self) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        assert isinstance(
            ContainerAppsTaskExecutor(
                settings=_make_settings(),
                result_store=MemoryResultStore(),
                task_queue=MemoryTaskQueue(),
            ),
            TaskExecutor,
        )


class TestDispatchPayload:
    """Verify only non-sensitive params are serialized for dispatch."""

    def test_payload_contains_only_task_params(self) -> None:
        from gitlab_copilot_agent.aca_executor import _build_dispatch_payload

        task = _make_task()
        payload = json.loads(_build_dispatch_payload(task, "repos/test.tar.gz"))
        keys = set(payload.keys())

        expected_keys = {
            "task_type",
            "task_id",
            "repo_blob_key",
            "system_prompt",
            "user_prompt",
            "plugins",
        }
        assert keys == expected_keys

        # Must NOT include secrets
        secret_keys = {"gitlab_token", "github_token", "copilot_provider_api_key"}
        assert keys.isdisjoint(secret_keys), f"Secrets in dispatch: {keys & secret_keys}"

    def test_payload_values_match_task(self) -> None:
        from gitlab_copilot_agent.aca_executor import _build_dispatch_payload

        task = _make_task()
        blob_key = "repos/test.tar.gz"
        payload = json.loads(_build_dispatch_payload(task, blob_key))
        assert payload["task_type"] == TASK_TYPE
        assert payload["task_id"] == TASK_ID
        assert payload["repo_blob_key"] == blob_key
        assert payload["user_prompt"] == USER_PROMPT


class TestCachedResult:
    async def test_returns_cached_result_without_enqueuing(self) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        await store.set(TASK_ID, json.dumps({"result_type": "review", "summary": CACHED_RESULT}))

        executor = ContainerAppsTaskExecutor(
            settings=_make_settings(), result_store=store, task_queue=queue
        )
        result = await executor.execute(_make_task())

        assert isinstance(result, ReviewResult)
        assert result.summary == CACHED_RESULT
        # Queue should be empty — nothing was enqueued
        assert await queue.dequeue() is None


class TestQueueExecution:
    @patch("gitlab_copilot_agent.aca_executor._JOB_POLL_INTERVAL", 0.01)
    async def test_enqueues_and_polls_result(self) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        review_json = json.dumps({"result_type": "review", "summary": "LGTM"})

        async def _set_result_later() -> None:
            await asyncio.sleep(0.02)
            await store.set(TASK_ID, review_json)

        executor = ContainerAppsTaskExecutor(
            settings=_make_settings(), result_store=store, task_queue=queue
        )

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_set_result_later())
            result = await executor.execute(_make_task())

        assert isinstance(result, ReviewResult)
        assert result.summary == "LGTM"

    async def test_coding_result_includes_patch(self) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        coding_json = json.dumps(
            {
                "result_type": "coding",
                "summary": "Added feature",
                "patch": "--- a/file.py\n+++ b/file.py\n@@ -1 +1,2 @@\n+new line",
                "base_sha": "abc123",
            }
        )
        await store.set(TASK_ID, coding_json)

        executor = ContainerAppsTaskExecutor(
            settings=_make_settings(), result_store=store, task_queue=queue
        )
        result = await executor.execute(_make_task(task_type="coding"))

        assert isinstance(result, CodingResult)
        assert result.patch.startswith("---")
        assert result.base_sha == "abc123"


class TestTimeout:
    @patch("gitlab_copilot_agent.aca_executor._JOB_POLL_INTERVAL", 0.01)
    async def test_raises_timeout_when_result_never_appears(self) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        executor = ContainerAppsTaskExecutor(
            settings=_make_settings(), result_store=store, task_queue=queue
        )

        with pytest.raises(TimeoutError, match="timed out"):
            await executor.execute(_make_task())


class TestParseResult:
    def test_parse_review_json(self) -> None:
        from gitlab_copilot_agent.aca_executor import _parse_result

        raw = json.dumps({"result_type": "review", "summary": "All good"})
        result = _parse_result(raw, "review")
        assert isinstance(result, ReviewResult)
        assert result.summary == "All good"

    def test_parse_coding_json(self) -> None:
        from gitlab_copilot_agent.aca_executor import _parse_result

        raw = json.dumps(
            {
                "result_type": "coding",
                "summary": "Fixed bug",
                "patch": "diff",
                "base_sha": "abc",
            }
        )
        result = _parse_result(raw, "coding")
        assert isinstance(result, CodingResult)
        assert result.patch == "diff"

    def test_parse_plain_string_as_review(self) -> None:
        from gitlab_copilot_agent.aca_executor import _parse_result

        result = _parse_result("plain text result", "review")
        assert isinstance(result, ReviewResult)
        assert result.summary == "plain text result"

    def test_parse_plain_string_as_coding(self) -> None:
        from gitlab_copilot_agent.aca_executor import _parse_result

        result = _parse_result("plain text result", "coding")
        assert isinstance(result, CodingResult)
        assert result.summary == "plain text result"

    def test_parse_empty_string(self) -> None:
        from gitlab_copilot_agent.aca_executor import _parse_result

        result = _parse_result("", "review")
        assert isinstance(result, ReviewResult)
        assert result.summary == ""

    def test_error_result_returns_review_with_message(self) -> None:
        from gitlab_copilot_agent.aca_executor import _parse_result

        error_msg = "Copilot session timed out after 30s"
        raw = json.dumps(
            {"result_type": "error", "error": True, "summary": f"Task failed: {error_msg}"}
        )
        result = _parse_result(raw, "coding")
        assert isinstance(result, ReviewResult)
        assert error_msg in result.summary
