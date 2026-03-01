"""Tests for ContainerAppsTaskExecutor."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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
REDIS_URL = "rediss://test-redis.redis.cache.windows.net:6380"
ACA_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
ACA_RESOURCE_GROUP = "rg-copilot-test"
ACA_JOB_NAME = "copilot-job"
ACA_JOB_TIMEOUT = 3  # short for tests
EXECUTION_NAME = "copilot-job-exec-abc123"
CACHED_RESULT = "cached review output"


# -- Helpers / fixtures ---------------------------------------------------


def _make_settings(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "redis_url": REDIS_URL,
        "state_backend": "redis",
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


def _install_azure_stub() -> tuple[MagicMock, MagicMock]:
    """Insert fake azure packages into sys.modules and return mocks.

    Returns (client_mock, jobs_mock).
    """
    identity_mod = types.ModuleType("azure.identity")
    azure_mod = types.ModuleType("azure")
    mgmt_mod = types.ModuleType("azure.mgmt")
    aca_mod = types.ModuleType("azure.mgmt.appcontainers")
    models_mod = types.ModuleType("azure.mgmt.appcontainers.models")

    credential_mock = MagicMock(name="DefaultAzureCredential")
    identity_mod.DefaultAzureCredential = credential_mock  # type: ignore[attr-defined]

    client_mock = MagicMock(name="ContainerAppsAPIClient")
    client_class_mock = MagicMock(return_value=client_mock)
    aca_mod.ContainerAppsAPIClient = client_class_mock  # type: ignore[attr-defined]

    # Model stubs that store kwargs
    class _Passthrough:
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    models_mod.JobExecutionTemplate = _Passthrough  # type: ignore[attr-defined]
    models_mod.JobExecutionContainer = _Passthrough  # type: ignore[attr-defined]

    sys.modules["azure"] = azure_mod
    sys.modules["azure.identity"] = identity_mod
    sys.modules["azure.mgmt"] = mgmt_mod
    sys.modules["azure.mgmt.appcontainers"] = aca_mod
    sys.modules["azure.mgmt.appcontainers.models"] = models_mod

    return client_mock, client_mock.jobs


@pytest.fixture(autouse=True)
def _azure_stubs() -> Any:  # noqa: ANN401
    """Install Azure stubs before each test and clean up after."""
    _install_azure_stub()
    yield
    for key in list(sys.modules):
        if key.startswith("azure"):
            del sys.modules[key]
    # Force reimport of aca_executor to clear cached module references
    sys.modules.pop("gitlab_copilot_agent.aca_executor", None)


@pytest.fixture
def client_mock() -> MagicMock:
    return sys.modules["azure.mgmt.appcontainers"].ContainerAppsAPIClient()  # type: ignore[union-attr]


@pytest.fixture
def jobs_mock(client_mock: MagicMock) -> MagicMock:
    return client_mock.jobs


@pytest.fixture
def fake_result_store() -> MagicMock:
    store = MagicMock()
    store.get = MagicMock(return_value=asyncio.coroutine(lambda *a: None)())
    return store


class MemoryResultStore:
    """Minimal in-memory result store for tests."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str, ttl: int = 0) -> None:
        self._data[key] = value

    async def aclose(self) -> None:
        pass


# -- Tests ----------------------------------------------------------------


class TestProtocolCompliance:
    def test_implements_task_executor_protocol(self) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        assert isinstance(
            ContainerAppsTaskExecutor(settings=_make_settings(), result_store=MemoryResultStore()),
            TaskExecutor,
        )


class TestEnvOverrides:
    """Verify only non-sensitive params are passed per-execution (S1)."""

    def test_env_overrides_contain_only_task_params(self) -> None:
        from gitlab_copilot_agent.aca_executor import _build_env_overrides

        task = _make_task()
        overrides = _build_env_overrides(task)
        names = {e["name"] for e in overrides}

        # Must include task params
        assert "TASK_TYPE" in names
        assert "TASK_ID" in names
        assert "REPO_URL" in names
        assert "BRANCH" in names
        assert "SYSTEM_PROMPT" in names
        assert "USER_PROMPT" in names
        assert "TASK_PAYLOAD" in names

        # Must NOT include secrets (S1 compliance)
        secret_names = {"GITLAB_TOKEN", "GITHUB_TOKEN", "COPILOT_PROVIDER_API_KEY", "REDIS_URL"}
        assert names.isdisjoint(secret_names), (
            f"Secrets leaked in env overrides: {names & secret_names}"
        )

    def test_env_overrides_values_match_task(self) -> None:
        from gitlab_copilot_agent.aca_executor import _build_env_overrides

        task = _make_task()
        overrides = {e["name"]: e["value"] for e in _build_env_overrides(task)}
        assert overrides["TASK_TYPE"] == TASK_TYPE
        assert overrides["TASK_ID"] == TASK_ID
        assert overrides["REPO_URL"] == REPO_URL
        assert overrides["BRANCH"] == BRANCH


class TestCachedResult:
    async def test_returns_cached_result_without_starting_job(self, jobs_mock: MagicMock) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        store = MemoryResultStore()
        await store.set(TASK_ID, json.dumps({"result_type": "review", "summary": CACHED_RESULT}))

        executor = ContainerAppsTaskExecutor(settings=_make_settings(), result_store=store)
        result = await executor.execute(_make_task())

        assert isinstance(result, ReviewResult)
        assert result.summary == CACHED_RESULT
        jobs_mock.begin_start.assert_not_called()


class TestJobExecution:
    @patch("gitlab_copilot_agent.aca_executor._JOB_POLL_INTERVAL", 0.01)
    async def test_starts_execution_and_polls_result(self, jobs_mock: MagicMock) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        poller = MagicMock()
        poller.result.return_value = MagicMock(name=EXECUTION_NAME)
        jobs_mock.begin_start.return_value = poller

        # Transition: Running → Succeeded (gives time for result to appear)
        running = MagicMock()
        running.properties.status = "Running"
        succeeded = MagicMock()
        succeeded.properties.status = "Succeeded"
        call_count = 0

        def _status_side_effect(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return running if call_count <= 1 else succeeded

        jobs_mock.get_execution.side_effect = _status_side_effect

        store = MemoryResultStore()
        review_json = json.dumps({"result_type": "review", "summary": "LGTM"})

        async def _set_result_later() -> None:
            await asyncio.sleep(0.01)
            await store.set(TASK_ID, review_json)

        executor = ContainerAppsTaskExecutor(settings=_make_settings(), result_store=store)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_set_result_later())
            result = await executor.execute(_make_task())

        assert isinstance(result, ReviewResult)
        assert result.summary == "LGTM"

    async def test_coding_result_includes_patch(self, jobs_mock: MagicMock) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        poller = MagicMock()
        poller.result.return_value = MagicMock(name=EXECUTION_NAME)
        jobs_mock.begin_start.return_value = poller

        execution = MagicMock()
        execution.properties.status = "Succeeded"
        jobs_mock.get_execution.return_value = execution

        store = MemoryResultStore()
        coding_json = json.dumps(
            {
                "result_type": "coding",
                "summary": "Added feature",
                "patch": "--- a/file.py\n+++ b/file.py\n@@ -1 +1,2 @@\n+new line",
                "base_sha": "abc123",
            }
        )
        await store.set(TASK_ID, coding_json)

        executor = ContainerAppsTaskExecutor(settings=_make_settings(), result_store=store)
        result = await executor.execute(_make_task(task_type="coding"))

        assert isinstance(result, CodingResult)
        assert result.patch.startswith("---")
        assert result.base_sha == "abc123"


class TestFailureHandling:
    async def test_raises_on_failed_execution(self, jobs_mock: MagicMock) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        poller = MagicMock()
        poller.result.return_value = MagicMock(name=EXECUTION_NAME)
        jobs_mock.begin_start.return_value = poller

        execution = MagicMock()
        execution.properties.status = "Failed"
        jobs_mock.get_execution.return_value = execution

        store = MemoryResultStore()
        executor = ContainerAppsTaskExecutor(settings=_make_settings(), result_store=store)

        with pytest.raises(RuntimeError, match="failed"):
            await executor.execute(_make_task())


class TestTimeout:
    @patch("gitlab_copilot_agent.aca_executor._JOB_POLL_INTERVAL", 0.01)
    async def test_raises_timeout_when_execution_does_not_complete(
        self, jobs_mock: MagicMock
    ) -> None:
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        poller = MagicMock()
        poller.result.return_value = MagicMock(name=EXECUTION_NAME)
        jobs_mock.begin_start.return_value = poller

        execution = MagicMock()
        execution.properties.status = "Running"
        jobs_mock.get_execution.return_value = execution

        store = MemoryResultStore()
        executor = ContainerAppsTaskExecutor(settings=_make_settings(), result_store=store)

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
