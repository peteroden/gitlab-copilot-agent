"""Tests for KubernetesTaskExecutor."""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import fakeredis.aioredis
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
REDIS_URL = "redis://localhost:6379/0"
K8S_NAMESPACE = "ci"
K8S_JOB_IMAGE = "registry.example.com/agent:latest"
K8S_JOB_CPU = "500m"
K8S_JOB_MEM = "512Mi"
K8S_JOB_TIMEOUT = 5  # short for tests
EXPECTED_JOB_NAME = "copilot-review-abc12345"
RESULT_KEY = f"result:{TASK_ID}"
CACHED_RESULT = "cached review output"
ANNOTATION_RESULT = "annotation fallback result"
POD_LOGS = "ERROR: something went wrong"


# -- Helpers / fixtures ---------------------------------------------------


def _make_settings(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "redis_url": REDIS_URL,
        "state_backend": "redis",
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


def _install_k8s_stub() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Insert a fake ``kubernetes`` package into sys.modules and return mocks.

    Returns (batch_v1_mock, core_v1_mock, config_mock).
    """
    k8s_mod = types.ModuleType("kubernetes")
    client_mod = types.ModuleType("kubernetes.client")
    config_mod = types.ModuleType("kubernetes.config")

    batch_v1 = MagicMock(name="BatchV1Api")
    core_v1 = MagicMock(name="CoreV1Api")
    config_mock = MagicMock(name="k8s_config")

    # client classes that return plain objects
    client_mod.BatchV1Api = MagicMock(return_value=batch_v1)  # type: ignore[attr-defined]
    client_mod.CoreV1Api = MagicMock(return_value=core_v1)  # type: ignore[attr-defined]
    # Passthrough constructors — just return kwargs as SimpleNamespace for inspection
    for cls_name in (
        "V1Job",
        "V1ObjectMeta",
        "V1JobSpec",
        "V1PodTemplateSpec",
        "V1PodSpec",
        "V1Container",
        "V1EnvVar",
        "V1ResourceRequirements",
        "V1SecurityContext",
        "V1Capabilities",
        "V1DeleteOptions",
    ):
        setattr(client_mod, cls_name, _passthrough_cls(cls_name))

    config_mod.load_incluster_config = config_mock.load_incluster_config  # type: ignore[attr-defined]
    config_mod.load_kube_config = config_mock.load_kube_config  # type: ignore[attr-defined]
    config_mod.ConfigException = Exception  # type: ignore[attr-defined]

    k8s_mod.client = client_mod  # type: ignore[attr-defined]
    k8s_mod.config = config_mod  # type: ignore[attr-defined]

    sys.modules["kubernetes"] = k8s_mod
    sys.modules["kubernetes.client"] = client_mod
    sys.modules["kubernetes.config"] = config_mod

    return batch_v1, core_v1, config_mock


def _passthrough_cls(name: str) -> type:
    """Return a tiny class that stores kwargs as attributes for test inspection."""

    def __init__(self: Any, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self: Any) -> str:
        attrs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"{name}({attrs})"

    return type(name, (), {"__init__": __init__, "__repr__": __repr__})


@pytest.fixture(autouse=True)
def _k8s_stubs() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Auto-install kubernetes stubs for every test; clean up after."""
    stubs = _install_k8s_stub()
    yield stubs
    for mod in ("kubernetes", "kubernetes.client", "kubernetes.config"):
        sys.modules.pop(mod, None)
    # Force re-import of k8s_executor to avoid stale module refs
    sys.modules.pop("gitlab_copilot_agent.k8s_executor", None)


@pytest.fixture
def batch_v1(_k8s_stubs: tuple[MagicMock, MagicMock, MagicMock]) -> MagicMock:
    return _k8s_stubs[0]


@pytest.fixture
def core_v1(_k8s_stubs: tuple[MagicMock, MagicMock, MagicMock]) -> MagicMock:
    return _k8s_stubs[1]


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis()


def _make_executor(
    settings: Any | None = None,
    redis_url: str = REDIS_URL,
) -> Any:
    from gitlab_copilot_agent.k8s_executor import KubernetesTaskExecutor

    return KubernetesTaskExecutor(settings=settings or _make_settings(), redis_url=redis_url)


# -- Tests ----------------------------------------------------------------


class TestProtocolCompliance:
    def test_implements_task_executor(self) -> None:
        executor = _make_executor()
        assert isinstance(executor, TaskExecutor)


class TestIdempotency:
    async def test_returns_cached_result(self, fake_redis: fakeredis.aioredis.FakeRedis) -> None:
        await fake_redis.set(RESULT_KEY, CACHED_RESULT)
        executor = _make_executor()

        with patch("redis.asyncio.from_url", return_value=fake_redis):
            result = await executor.execute(_make_task())

        assert result == CACHED_RESULT


class TestJobCreation:
    async def test_creates_job_with_correct_spec(
        self,
        batch_v1: MagicMock,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # Make job succeed immediately
        status_mock = MagicMock()
        status_mock.succeeded = 1
        status_mock.failed = None
        job_mock = MagicMock()
        job_mock.status = status_mock
        job_mock.metadata = MagicMock()
        job_mock.metadata.annotations = {}
        batch_v1.read_namespaced_job.return_value = job_mock

        executor = _make_executor()
        with patch("redis.asyncio.from_url", return_value=fake_redis):
            await executor.execute(_make_task())

        # Verify Job was created
        batch_v1.create_namespaced_job.assert_called_once()
        call_kwargs = batch_v1.create_namespaced_job.call_args
        assert call_kwargs.kwargs["namespace"] == K8S_NAMESPACE

        job_body = call_kwargs.kwargs["body"]
        assert job_body.metadata.name == EXPECTED_JOB_NAME
        assert job_body.spec.backoff_limit == 1
        assert job_body.spec.ttl_seconds_after_finished == 300

        container = job_body.spec.template.spec.containers[0]
        assert container.image == K8S_JOB_IMAGE
        expected_cmd = ["uv", "run", "python", "-m", "gitlab_copilot_agent.task_runner"]
        assert container.command == expected_cmd
        assert container.resources.limits["cpu"] == K8S_JOB_CPU
        assert container.resources.limits["memory"] == K8S_JOB_MEM
        assert container.security_context.run_as_non_root is True
        assert container.security_context.read_only_root_filesystem is True
        assert container.security_context.capabilities.drop == ["ALL"]

    async def test_env_vars_include_task_and_auth(
        self,
        batch_v1: MagicMock,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        status_mock = MagicMock()
        status_mock.succeeded = 1
        status_mock.failed = None
        job_mock = MagicMock()
        job_mock.status = status_mock
        job_mock.metadata = MagicMock()
        job_mock.metadata.annotations = {}
        batch_v1.read_namespaced_job.return_value = job_mock

        executor = _make_executor()
        with patch("redis.asyncio.from_url", return_value=fake_redis):
            await executor.execute(_make_task())

        job_body = batch_v1.create_namespaced_job.call_args
        container = job_body.kwargs["body"].spec.template.spec.containers[0]
        env_names = {e.name for e in container.env}

        expected_vars = (
            "TASK_TYPE",
            "TASK_ID",
            "REPO_URL",
            "BRANCH",
            "SYSTEM_PROMPT",
            "USER_PROMPT",
            "TASK_PAYLOAD",
            "GITLAB_URL",
            "GITLAB_TOKEN",
            "GITHUB_TOKEN",
            "REDIS_URL",
        )
        for expected in expected_vars:
            assert expected in env_names, f"Missing env var: {expected}"


class TestCompletion:
    async def test_returns_result_from_redis(
        self,
        batch_v1: MagicMock,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        call_count = 0

        def _status_side_effect(*_a: Any, **_kw: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            job = MagicMock()
            if call_count >= 2:
                job.status.succeeded = 1
                job.status.failed = None
            else:
                job.status.succeeded = None
                job.status.failed = None
            job.metadata.annotations = {}
            return job

        batch_v1.read_namespaced_job.side_effect = _status_side_effect

        # Simulate result appearing in Redis after first poll
        async def _set_result_later() -> None:
            await asyncio.sleep(0.05)
            await fake_redis.set(RESULT_KEY, CACHED_RESULT)

        executor = _make_executor()
        with patch("redis.asyncio.from_url", return_value=fake_redis):
            asyncio.create_task(_set_result_later())
            result = await executor.execute(_make_task())

        assert result == CACHED_RESULT

    async def test_falls_back_to_annotation(
        self,
        batch_v1: MagicMock,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        job_mock = MagicMock()
        job_mock.status.succeeded = 1
        job_mock.status.failed = None
        job_mock.metadata.annotations = {"results.copilot-agent/summary": ANNOTATION_RESULT}
        batch_v1.read_namespaced_job.return_value = job_mock

        executor = _make_executor()
        with patch("redis.asyncio.from_url", return_value=fake_redis):
            result = await executor.execute(_make_task())

        assert result == ANNOTATION_RESULT


class TestFailureHandling:
    async def test_raises_on_job_failure(
        self,
        batch_v1: MagicMock,
        core_v1: MagicMock,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        job_mock = MagicMock()
        job_mock.status.succeeded = None
        job_mock.status.failed = 1
        batch_v1.read_namespaced_job.return_value = job_mock

        pod_mock = MagicMock()
        pod_mock.metadata.name = "copilot-review-abc12345-xyz"
        core_v1.list_namespaced_pod.return_value = MagicMock(items=[pod_mock])
        core_v1.read_namespaced_pod_log.return_value = POD_LOGS

        executor = _make_executor()
        with (
            patch("redis.asyncio.from_url", return_value=fake_redis),
            pytest.raises(RuntimeError, match="failed"),
        ):
            await executor.execute(_make_task())

    async def test_failure_includes_pod_logs(
        self,
        batch_v1: MagicMock,
        core_v1: MagicMock,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        job_mock = MagicMock()
        job_mock.status.succeeded = None
        job_mock.status.failed = 1
        batch_v1.read_namespaced_job.return_value = job_mock

        pod_mock = MagicMock()
        pod_mock.metadata.name = "copilot-review-abc12345-pod"
        core_v1.list_namespaced_pod.return_value = MagicMock(items=[pod_mock])
        core_v1.read_namespaced_pod_log.return_value = POD_LOGS

        executor = _make_executor()
        with (
            patch("redis.asyncio.from_url", return_value=fake_redis),
            pytest.raises(RuntimeError, match=POD_LOGS),
        ):
            await executor.execute(_make_task())


class TestTimeout:
    async def test_deletes_job_on_timeout(
        self,
        batch_v1: MagicMock,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # Job stays running forever
        job_mock = MagicMock()
        job_mock.status.succeeded = None
        job_mock.status.failed = None
        batch_v1.read_namespaced_job.return_value = job_mock

        settings = _make_settings(k8s_job_timeout=1)
        executor = _make_executor(settings=settings)

        with (
            patch("redis.asyncio.from_url", return_value=fake_redis),
            patch("gitlab_copilot_agent.k8s_executor._JOB_POLL_INTERVAL", 0.1),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            await executor.execute(_make_task(settings=settings))

        batch_v1.delete_namespaced_job.assert_called_once()


class TestJobNameSanitization:
    def test_basic_name(self) -> None:
        from gitlab_copilot_agent.k8s_executor import _sanitize_job_name

        assert _sanitize_job_name("review", "abc12345-rest") == "copilot-review-abc12345"

    def test_uppercased_input(self) -> None:
        from gitlab_copilot_agent.k8s_executor import _sanitize_job_name

        assert _sanitize_job_name("REVIEW", "ABC12345") == "copilot-review-abc12345"

    def test_special_chars_replaced(self) -> None:
        from gitlab_copilot_agent.k8s_executor import _sanitize_job_name

        result = _sanitize_job_name("review", "a@b#c$d%")
        assert result == "copilot-review-a-b-c-d-"[:63].rstrip("-")
