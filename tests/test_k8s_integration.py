"""Integration tests for k8s Job dispatch — requires a running k3d cluster.

Run:  pytest tests/test_k8s_integration.py -m k8s
Skip: pytest -m 'not k8s'  (default via pyproject.toml addopts)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
JOB_IMAGE = "gitlab-copilot-agent:local"
TIMEOUT_S = 60


# -- fixtures ----------------------------------------------------------------
@pytest.fixture
def task_id() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


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


def _echo_env(task_id: str, prompt: str) -> list[k8s.client.V1EnvVar]:
    E = k8s.client.V1EnvVar
    return [
        E(name="TASK_TYPE", value="echo"),
        E(name="TASK_ID", value=task_id),
        E(name="REPO_URL", value="https://unused.test/r.git"),
        E(name="BRANCH", value="main"),
        E(name="TASK_PAYLOAD", value=json.dumps({"prompt": prompt})),
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


# -- tests -------------------------------------------------------------------
class TestJobDispatch:
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
