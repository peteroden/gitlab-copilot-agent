"""Tests for KubernetesTaskExecutor."""

from __future__ import annotations

from typing import Any

import pytest

from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams
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
CACHED_RESULT = "cached review output"


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
