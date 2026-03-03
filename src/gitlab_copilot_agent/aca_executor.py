"""ContainerAppsTaskExecutor — dispatches tasks as Azure Container Apps Job executions."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.task_executor import CodingResult, ReviewResult, TaskResult

if TYPE_CHECKING:
    from azure.mgmt.appcontainers import ContainerAppsAPIClient

    from gitlab_copilot_agent.concurrency import ResultStore
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.task_executor import TaskParams

log = structlog.get_logger()

_JOB_POLL_INTERVAL = 5  # seconds; ACA API is slower than k8s, use longer interval
_EXECUTION_LOCK_TTL = 900  # sentinel TTL to prevent duplicate executions
_EXECUTION_LOCK_PREFIX = "aca_exec:"
_DISPATCH_QUEUE = "task_dispatch_queue"


def _build_dispatch_payload(task: TaskParams) -> str:
    """Serialize task params for Redis dispatch queue.

    Only non-sensitive params are stored (S1: secrets live on the Job template
    as Key Vault references and are never passed through Redis).
    """
    return json.dumps(
        {
            "task_type": task.task_type,
            "task_id": task.task_id,
            "repo_url": task.repo_url,
            "branch": task.branch,
            "system_prompt": task.system_prompt,
            "user_prompt": task.user_prompt,
        }
    )


def _parse_result(raw: str, task_type: str) -> TaskResult:
    """Parse a raw result string into a structured TaskResult."""
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


class ContainerAppsTaskExecutor:
    """Dispatches tasks as Azure Container Apps Job executions.

    Task params are dispatched via a Redis queue. The job is started with no
    per-execution template override (ACA replaces rather than merges), so the
    base template's command, env vars, and Key Vault secret refs are preserved.
    """

    def __init__(self, settings: Settings, result_store: ResultStore) -> None:
        self._settings = settings
        self._store = result_store

    async def execute(self, task: TaskParams) -> TaskResult:
        cached = await self._store.get(task.task_id)
        if cached is not None:
            return _parse_result(cached, task.task_type)

        # Idempotency: check if another worker already started this task.
        # Unlike k8s Jobs (deterministic names + 409 conflict), ACA Jobs
        # always create new executions, so we use a Redis sentinel.
        lock_key = f"{_EXECUTION_LOCK_PREFIX}{task.task_id}"
        existing = await self._store.get(lock_key)
        if existing is not None:
            log.info("aca_execution_already_started", task_id=task.task_id)
            return await self._wait_for_result(existing, task)

        # Write task params to Redis so the job can read them on startup.
        # ACA begin_start(template=...) does a full REPLACE (not merge),
        # so we pass no template and let the base job template run as-is.
        payload = _build_dispatch_payload(task)
        await self._store.push_task(_DISPATCH_QUEUE, payload)

        try:
            execution_name = await asyncio.to_thread(self._start_execution, task)
        except Exception:
            # Compensating cleanup: remove the specific queued payload on failure
            await self._store.remove_task(_DISPATCH_QUEUE, payload)
            raise

        await self._store.set(lock_key, execution_name, ttl=_EXECUTION_LOCK_TTL)
        return await self._wait_for_result(execution_name, task)

    def _create_client(self) -> ContainerAppsAPIClient:
        """Create a fresh Azure Container Apps management client."""
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.appcontainers import ContainerAppsAPIClient as _Client

        credential = DefaultAzureCredential()
        return _Client(
            credential=credential,
            subscription_id=self._settings.aca_subscription_id,
        )

    def _start_execution(self, task: TaskParams) -> str:
        """Start a Container Apps Job execution (synchronous, called via to_thread).

        No per-execution template is passed — the base job template (with KV
        secret refs, command, and base env vars) is used as-is. Task params
        are dispatched via Redis instead.
        """
        client = self._create_client()

        bound_log = log.bind(
            task_id=task.task_id,
            task_type=task.task_type,
            job_name=self._settings.aca_job_name,
        )
        bound_log.info("aca_job_starting")

        poller = client.jobs.begin_start(
            resource_group_name=self._settings.aca_resource_group,
            job_name=self._settings.aca_job_name,
        )
        result = poller.result()
        execution_name = str(result.name or "unknown")
        bound_log.info("aca_job_started", execution_name=execution_name)
        return execution_name

    def _get_execution_status(self, execution_name: str) -> str:
        """Read execution status (synchronous, called via to_thread)."""
        client = self._create_client()
        for execution in client.jobs_executions.list(
            resource_group_name=self._settings.aca_resource_group,
            job_name=self._settings.aca_job_name,
        ):
            if execution.name == execution_name:
                return str(execution.status or "Unknown")
        return "Unknown"

    async def _wait_for_result(self, execution_name: str, task: TaskParams) -> TaskResult:
        """Poll execution status and read result from Redis."""
        deadline = asyncio.get_event_loop().time() + self._settings.aca_job_timeout

        while asyncio.get_event_loop().time() < deadline:
            cached = await self._store.get(task.task_id)
            if cached is not None:
                return _parse_result(cached, task.task_type)

            status = await asyncio.to_thread(self._get_execution_status, execution_name)

            if status == "Succeeded":
                cached = await self._store.get(task.task_id)
                if cached is not None:
                    return _parse_result(cached, task.task_type)
                log.warning(
                    "aca_job_succeeded_no_result",
                    execution_name=execution_name,
                    task_id=task.task_id,
                )
                return _parse_result("", task.task_type)

            if status == "Failed":
                msg = (
                    f"Container Apps Job execution {execution_name} failed. "
                    f"Check Azure Portal logs for details."
                )
                raise RuntimeError(msg)

            await asyncio.sleep(_JOB_POLL_INTERVAL)

        msg = (
            f"Container Apps Job execution {execution_name} timed out "
            f"after {self._settings.aca_job_timeout}s"
        )
        raise TimeoutError(msg)
