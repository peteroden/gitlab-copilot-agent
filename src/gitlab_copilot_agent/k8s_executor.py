"""KubernetesTaskExecutor — dispatches tasks as k8s Jobs, reads results via ResultStore."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.task_executor import CodingResult, ReviewResult, TaskResult

if TYPE_CHECKING:
    from gitlab_copilot_agent.concurrency import ResultStore
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.task_executor import TaskParams

log = structlog.get_logger()

_JOB_POLL_INTERVAL = 2  # seconds between status checks
_TTL_AFTER_FINISHED = 300
_ANNOTATION_KEY = "results.copilot-agent/summary"


def _sanitize_job_name(task_type: str, task_id: str) -> str:
    """Build a k8s-compliant Job name using a hash of the task_id."""
    id_hash = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    task_type = re.sub(r"[^a-z0-9\-]", "-", task_type.lower()).strip("-")
    return f"copilot-{task_type}-{id_hash}"[:63]


def _parse_host_aliases(raw: str) -> list[object] | None:
    """Parse JSON hostAliases string into k8s V1HostAlias objects."""
    if not raw.strip():
        return None
    from kubernetes import client as k8s  # type: ignore[import-not-found,import-untyped,unused-ignore]  # noqa: I001

    entries = json.loads(raw)
    return [k8s.V1HostAlias(ip=e["ip"], hostnames=e["hostnames"]) for e in entries]


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
        {"name": "GITLAB_WEBHOOK_SECRET", "value": settings.gitlab_webhook_secret},
        # Writable cache dirs for read-only root filesystem
        {"name": "UV_CACHE_DIR", "value": "/tmp/.uv-cache"},
        {"name": "XDG_CACHE_HOME", "value": "/tmp/.cache"},
        {"name": "HOME", "value": "/tmp"},
        # src/ layout needs explicit PYTHONPATH when not using uv run
        {"name": "PYTHONPATH", "value": "/home/app/app/src"},
    ]
    if settings.redis_url:
        env.append({"name": "REDIS_URL", "value": settings.redis_url})
    if settings.github_token:
        env.append({"name": "GITHUB_TOKEN", "value": settings.github_token})
    if settings.copilot_provider_type:
        env.append({"name": "COPILOT_PROVIDER_TYPE", "value": settings.copilot_provider_type})
    if settings.copilot_provider_base_url:
        env.append(
            {
                "name": "COPILOT_PROVIDER_BASE_URL",
                "value": settings.copilot_provider_base_url,
            }
        )
    if settings.copilot_provider_api_key:
        env.append(
            {
                "name": "COPILOT_PROVIDER_API_KEY",
                "value": settings.copilot_provider_api_key,
            }
        )
    if settings.copilot_model:
        env.append({"name": "COPILOT_MODEL", "value": settings.copilot_model})
    if os.environ.get("ALLOW_HTTP_CLONE"):
        env.append({"name": "ALLOW_HTTP_CLONE", "value": os.environ["ALLOW_HTTP_CLONE"]})
    return env


class KubernetesTaskExecutor:
    """Dispatches tasks as Kubernetes Jobs and retrieves results via ResultStore."""

    def __init__(self, settings: Settings, result_store: ResultStore) -> None:
        self._settings = settings
        self._store = result_store

    async def execute(self, task: TaskParams) -> TaskResult:
        # Idempotency: return cached result if present
        cached = await self._store.get(task.task_id)
        if cached is not None:
            return _parse_result(cached, task.task_type)

        job_name = _sanitize_job_name(task.task_type, task.task_id)
        try:
            await asyncio.to_thread(self._create_job, job_name, task)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) != 409:
                raise
            log.info("job_already_exists", job_name=job_name)
        return await self._wait_for_result(job_name, task)

    # -- k8s helpers (synchronous, called via to_thread) ------------------

    def _load_config(self) -> None:
        from kubernetes import config as k8s_config  # type: ignore[import-not-found,import-untyped,unused-ignore]  # noqa: I001

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

        tmp_volume = k8s.V1Volume(name="tmp", empty_dir=k8s.V1EmptyDirVolumeSource())
        tmp_mount = k8s.V1VolumeMount(name="tmp", mount_path="/tmp")

        container = k8s.V1Container(
            name="task",
            image=self._settings.k8s_job_image,
            command=[".venv/bin/python", "-m", "gitlab_copilot_agent.task_runner"],
            env=env,
            volume_mounts=[tmp_mount],
            resources=k8s.V1ResourceRequirements(
                limits={
                    "cpu": self._settings.k8s_job_cpu_limit,
                    "memory": self._settings.k8s_job_memory_limit,
                },
            ),
            security_context=k8s.V1SecurityContext(
                run_as_non_root=True,
                run_as_user=1000,
                read_only_root_filesystem=True,
                capabilities=k8s.V1Capabilities(drop=["ALL"]),
            ),
        )

        job = k8s.V1Job(
            metadata=k8s.V1ObjectMeta(name=job_name, namespace=ns),
            spec=k8s.V1JobSpec(
                template=k8s.V1PodTemplateSpec(
                    spec=k8s.V1PodSpec(
                        containers=[container],
                        volumes=[tmp_volume],
                        restart_policy="Never",
                        host_aliases=_parse_host_aliases(self._settings.k8s_job_host_aliases),
                    ),
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
        try:
            job = k8s.BatchV1Api().read_namespaced_job(name=job_name, namespace=ns)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 404:
                return "failed"  # Job deleted by concurrent caller or TTL
            raise
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

    async def _wait_for_result(self, job_name: str, task: TaskParams) -> TaskResult:
        deadline = asyncio.get_event_loop().time() + self._settings.k8s_job_timeout

        while asyncio.get_event_loop().time() < deadline:
            cached = await self._store.get(task.task_id)
            if cached is not None:
                return _parse_result(cached, task.task_type)

            status = await asyncio.to_thread(self._read_job_status, job_name)

            if status == "succeeded":
                cached = await self._store.get(task.task_id)
                if cached is not None:
                    return _parse_result(cached, task.task_type)
                annotation = await asyncio.to_thread(self._read_job_annotation, job_name)
                if annotation:
                    return _parse_result(annotation, task.task_type)
                return _parse_result("", task.task_type)

            if status == "failed":
                logs = await asyncio.to_thread(self._read_pod_logs, job_name)
                try:
                    await asyncio.to_thread(self._delete_job, job_name)
                except Exception:  # noqa: BLE001
                    log.warning("failed_job_cleanup_error", job_name=job_name, exc_info=True)
                msg = f"Job {job_name} failed. Pod logs:\n{logs}"
                raise RuntimeError(msg)

            await asyncio.sleep(_JOB_POLL_INTERVAL)

        # Timeout — clean up and raise
        await asyncio.to_thread(self._delete_job, job_name)
        msg = f"Job {job_name} timed out after {self._settings.k8s_job_timeout}s"
        raise TimeoutError(msg)


def _parse_result(raw: str, task_type: str) -> TaskResult:
    """Parse a raw result string into a structured TaskResult.

    If the string is valid JSON with a ``result_type`` field, parse it directly.
    Otherwise wrap the raw string as a summary in the appropriate result type.
    """
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "result_type" in data:
            if data["result_type"] == "coding":
                return CodingResult.model_validate(data)
            return ReviewResult.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        pass
    if task_type == "coding":
        return CodingResult(summary=raw)
    return ReviewResult(summary=raw)
