"""Integration tests for k8s Job dispatch — requires a running k3d cluster.

Run:  pytest tests/test_k8s_integration.py -m k8s
Skip: pytest -m 'not k8s'  (default via pyproject.toml addopts)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import uuid

import pytest

k8s = pytest.importorskip("kubernetes")


def _cluster_available() -> bool:
    try:
        k8s.config.load_kube_config()
        k8s.client.CoreV1Api().list_namespace(_request_timeout=3)
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.k8s,
    pytest.mark.skipif(not _cluster_available(), reason="No k8s cluster"),
]

NS = "default"
REDIS_SVC = "copilot-agent-gitlab-copilot-agent-redis"
JOB_IMAGE = "gitlab-copilot-agent:local"
RESULT_PREFIX = "result:"
TIMEOUT_S = 60
FWD_PORT = 16379


# -- fixtures ----------------------------------------------------------------
@pytest.fixture
def task_id() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def redis_cluster_url() -> str:
    return f"redis://{REDIS_SVC}.{NS}.svc.cluster.local:6379"


@pytest.fixture(scope="module")
def redis_local_url() -> str:
    proc = subprocess.Popen(
        ["kubectl", "port-forward", f"svc/{REDIS_SVC}", f"{FWD_PORT}:6379", "-n", NS],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=0.5)
    yield f"redis://localhost:{FWD_PORT}"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def batch_api() -> k8s.client.BatchV1Api:
    k8s.config.load_kube_config()
    return k8s.client.BatchV1Api()


# -- helpers -----------------------------------------------------------------
def _make_job(name: str, env: list[k8s.client.V1EnvVar]) -> k8s.client.V1Job:
    return k8s.client.V1Job(
        metadata=k8s.client.V1ObjectMeta(name=name, namespace=NS),
        spec=k8s.client.V1JobSpec(
            template=k8s.client.V1PodTemplateSpec(
                spec=k8s.client.V1PodSpec(
                    containers=[
                        k8s.client.V1Container(
                            name="task",
                            image=JOB_IMAGE,
                            command=[
                                "uv",
                                "run",
                                "python",
                                "-m",
                                "gitlab_copilot_agent.task_runner",
                            ],
                            env=env,
                            resources=k8s.client.V1ResourceRequirements(
                                limits={"cpu": "500m", "memory": "512Mi"}
                            ),
                        )
                    ],
                    restart_policy="Never",
                )
            ),
            backoff_limit=0,
            ttl_seconds_after_finished=60,
        ),
    )


def _echo_env(task_id: str, redis_url: str, prompt: str) -> list[k8s.client.V1EnvVar]:
    E = k8s.client.V1EnvVar
    return [
        E(name="TASK_TYPE", value="echo"),
        E(name="TASK_ID", value=task_id),
        E(name="REPO_URL", value="https://unused.test/r.git"),
        E(name="BRANCH", value="main"),
        E(name="TASK_PAYLOAD", value=json.dumps({"prompt": prompt})),
        E(name="REDIS_URL", value=redis_url),
    ]


def _delete_job(api: k8s.client.BatchV1Api, name: str) -> None:
    with contextlib.suppress(k8s.client.ApiException):
        api.delete_namespaced_job(
            name=name,
            namespace=NS,
            body=k8s.client.V1DeleteOptions(propagation_policy="Background"),
        )


async def _wait_job(api: k8s.client.BatchV1Api, name: str) -> str:
    for _ in range(TIMEOUT_S):
        j = await asyncio.to_thread(api.read_namespaced_job, name=name, namespace=NS)
        if j.status and j.status.succeeded:
            return "succeeded"
        if j.status and j.status.failed:
            return "failed"
        await asyncio.sleep(1)
    return "timeout"


async def _redis_get(url: str, task_id: str) -> str | None:
    import redis.asyncio as aioredis

    c = aioredis.from_url(url)
    try:
        v = await c.get(f"{RESULT_PREFIX}{task_id}")
        return v.decode() if v else None
    finally:
        await c.aclose()


async def _redis_del(url: str, task_id: str) -> None:
    import redis.asyncio as aioredis

    c = aioredis.from_url(url)
    try:
        await c.delete(f"{RESULT_PREFIX}{task_id}")
    finally:
        await c.aclose()


# -- tests -------------------------------------------------------------------
class TestJobDispatch:
    async def test_echo_job_writes_result(
        self,
        batch_api: k8s.client.BatchV1Api,
        task_id: str,
        redis_cluster_url: str,
        redis_local_url: str,
    ) -> None:
        job_name, prompt = f"echo-{task_id}", "integration test payload"
        try:
            env = _echo_env(task_id, redis_cluster_url, prompt)
            batch_api.create_namespaced_job(namespace=NS, body=_make_job(job_name, env))
            assert await _wait_job(batch_api, job_name) == "succeeded"
            raw = await _redis_get(redis_local_url, task_id)
            assert raw is not None, "No result in Redis"
            data = json.loads(raw)
            assert data["task_id"] == task_id
            assert data["echo"] == prompt
        finally:
            _delete_job(batch_api, job_name)
            await _redis_del(redis_local_url, task_id)

    async def test_job_failure_on_missing_env(
        self,
        batch_api: k8s.client.BatchV1Api,
        task_id: str,
    ) -> None:
        job_name = f"fail-{task_id}"
        env = [k8s.client.V1EnvVar(name="TASK_ID", value=task_id)]
        try:
            batch_api.create_namespaced_job(namespace=NS, body=_make_job(job_name, env))
            assert await _wait_job(batch_api, job_name) == "failed"
        finally:
            _delete_job(batch_api, job_name)
