"""KubernetesTaskExecutor — dispatches tasks as k8s Jobs, reads results from Redis."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.task_executor import TaskParams

log = structlog.get_logger()

_RESULT_KEY_PREFIX = "result:"
_JOB_POLL_INTERVAL = 2  # seconds between status checks
_TTL_AFTER_FINISHED = 300
_ANNOTATION_KEY = "results.copilot-agent/summary"


def _sanitize_job_name(task_type: str, task_id: str) -> str:
    """Build a k8s-compliant Job name: ``copilot-{task_type}-{task_id[:8]}``."""
    raw = f"copilot-{task_type}-{task_id[:8]}"
    sanitized = re.sub(r"[^a-z0-9\-]", "-", raw.lower())
    return sanitized.strip("-")[:63]


def _build_env(task: TaskParams, settings: Settings) -> list[dict[str, str]]:
    """Return env var dicts for the Job container."""
    env = [
        {"name": "TASK_TYPE", "value": task.task_type},
        {"name": "TASK_ID", "value": task.task_id},
        {"name": "REPO_URL", "value": task.repo_url},
        {"name": "BRANCH", "value": task.branch},
        {"name": "SYSTEM_PROMPT", "value": task.system_prompt},
        {"name": "USER_PROMPT", "value": task.user_prompt},
        {"name": "TASK_PAYLOAD", "value": json.dumps({"prompt": task.user_prompt})},
        {"name": "GITLAB_URL", "value": settings.gitlab_url},
        {"name": "GITLAB_TOKEN", "value": settings.gitlab_token},
    ]
    if settings.redis_url:
        env.append({"name": "REDIS_URL", "value": settings.redis_url})
    if settings.github_token:
        env.append({"name": "GITHUB_TOKEN", "value": settings.github_token})
    if settings.copilot_provider_base_url:
        env.append({"name": "COPILOT_LLM_URL", "value": settings.copilot_provider_base_url})
    return env


class KubernetesTaskExecutor:
    """Dispatches tasks as Kubernetes Jobs and retrieves results from Redis."""

    def __init__(self, settings: Settings, redis_url: str) -> None:
        self._settings = settings
        self._redis_url = redis_url

    async def execute(self, task: TaskParams) -> str:
        import redis.asyncio as aioredis

        client: Redis = aioredis.from_url(self._redis_url)
        try:
            # Idempotency: return cached result if present
            cached = await client.get(f"{_RESULT_KEY_PREFIX}{task.task_id}")
            if cached is not None:
                return cached.decode() if isinstance(cached, bytes) else str(cached)

            job_name = _sanitize_job_name(task.task_type, task.task_id)
            await asyncio.to_thread(self._create_job, job_name, task)
            return await self._wait_for_result(client, job_name, task)
        finally:
            await client.aclose()

    # -- k8s helpers (synchronous, called via to_thread) ------------------

    def _load_config(self) -> None:
        from kubernetes import config as k8s_config  # type: ignore[import-not-found]

        try:
            k8s_config.load_incluster_config()
        except Exception:  # noqa: BLE001 – fallback to kubeconfig
            k8s_config.load_kube_config()

    def _create_job(self, job_name: str, task: TaskParams) -> None:
        from kubernetes import client as k8s

        self._load_config()
        ns = self._settings.k8s_namespace
        env = [
            k8s.V1EnvVar(name=e["name"], value=e["value"])
            for e in _build_env(task, self._settings)
        ]

        container = k8s.V1Container(
            name="task",
            image=self._settings.k8s_job_image,
            command=["python", "-m", "gitlab_copilot_agent.task_runner"],
            env=env,
            resources=k8s.V1ResourceRequirements(
                limits={
                    "cpu": self._settings.k8s_job_cpu_limit,
                    "memory": self._settings.k8s_job_memory_limit,
                },
            ),
            security_context=k8s.V1SecurityContext(
                run_as_non_root=True,
                read_only_root_filesystem=True,
                capabilities=k8s.V1Capabilities(drop=["ALL"]),
            ),
        )

        job = k8s.V1Job(
            metadata=k8s.V1ObjectMeta(name=job_name, namespace=ns),
            spec=k8s.V1JobSpec(
                template=k8s.V1PodTemplateSpec(
                    spec=k8s.V1PodSpec(containers=[container], restart_policy="Never"),
                ),
                backoff_limit=1,
                ttl_seconds_after_finished=_TTL_AFTER_FINISHED,
            ),
        )
        k8s.BatchV1Api().create_namespaced_job(namespace=ns, body=job)

    def _read_job_status(self, job_name: str) -> str:
        """Return 'succeeded', 'failed', or 'running'."""
        from kubernetes import client as k8s

        ns = self._settings.k8s_namespace
        job = k8s.BatchV1Api().read_namespaced_job(name=job_name, namespace=ns)
        if job.status and job.status.succeeded:
            return "succeeded"
        if job.status and job.status.failed:
            return "failed"
        return "running"

    def _read_job_annotation(self, job_name: str) -> str | None:
        from kubernetes import client as k8s

        ns = self._settings.k8s_namespace
        job = k8s.BatchV1Api().read_namespaced_job(name=job_name, namespace=ns)
        if job.metadata and job.metadata.annotations:
            result: str | None = job.metadata.annotations.get(_ANNOTATION_KEY)
            return result
        return None

    def _read_pod_logs(self, job_name: str) -> str:
        from kubernetes import client as k8s

        ns = self._settings.k8s_namespace
        pods = k8s.CoreV1Api().list_namespaced_pod(
            namespace=ns, label_selector=f"job-name={job_name}"
        )
        if not pods.items:
            return "<no pods found>"
        pod_name: str = pods.items[0].metadata.name
        return k8s.CoreV1Api().read_namespaced_pod_log(name=pod_name, namespace=ns) or ""

    def _delete_job(self, job_name: str) -> None:
        from kubernetes import client as k8s

        k8s.BatchV1Api().delete_namespaced_job(
            name=job_name,
            namespace=self._settings.k8s_namespace,
            body=k8s.V1DeleteOptions(propagation_policy="Background"),
        )

    # -- async polling ----------------------------------------------------

    async def _wait_for_result(self, redis_client: Redis, job_name: str, task: TaskParams) -> str:
        deadline = asyncio.get_event_loop().time() + self._settings.k8s_job_timeout

        while asyncio.get_event_loop().time() < deadline:
            # Check Redis first
            cached = await redis_client.get(f"{_RESULT_KEY_PREFIX}{task.task_id}")
            if cached is not None:
                return cached.decode() if isinstance(cached, bytes) else str(cached)

            status = await asyncio.to_thread(self._read_job_status, job_name)

            if status == "succeeded":
                cached = await redis_client.get(f"{_RESULT_KEY_PREFIX}{task.task_id}")
                if cached is not None:
                    return cached.decode() if isinstance(cached, bytes) else str(cached)
                annotation = await asyncio.to_thread(self._read_job_annotation, job_name)
                if annotation:
                    return annotation
                return ""

            if status == "failed":
                logs = await asyncio.to_thread(self._read_pod_logs, job_name)
                msg = f"Job {job_name} failed. Pod logs:\n{logs}"
                raise RuntimeError(msg)

            await asyncio.sleep(_JOB_POLL_INTERVAL)

        # Timeout — clean up and raise
        await asyncio.to_thread(self._delete_job, job_name)
        msg = f"Job {job_name} timed out after {self._settings.k8s_job_timeout}s"
        raise TimeoutError(msg)
