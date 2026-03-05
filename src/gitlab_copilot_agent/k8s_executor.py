"""KubernetesTaskExecutor — dispatches tasks as k8s Jobs, reads results via ResultStore."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.task_executor import CodingResult, ReviewResult, TaskResult

if TYPE_CHECKING:
    from gitlab_copilot_agent.concurrency import ResultStore, TaskQueue
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.task_executor import TaskParams

log = structlog.get_logger()

_JOB_POLL_INTERVAL = 2  # seconds between status checks


class KubernetesTaskExecutor:
    """Dispatches tasks as Kubernetes Jobs and retrieves results via ResultStore.

    Enqueues tasks via Azure Storage Queue and lets KEDA create Jobs
    automatically.
    """

    def __init__(
        self,
        settings: Settings,
        result_store: ResultStore,
        task_queue: TaskQueue,
    ) -> None:
        self._settings = settings
        self._store = result_store
        self._task_queue = task_queue

    async def execute(self, task: TaskParams) -> TaskResult:
        # Idempotency: return cached result if present
        cached = await self._store.get(task.task_id)
        if cached is not None:
            return _parse_result(cached, task.task_type)

        return await self._execute_via_queue(task)

    async def _execute_via_queue(self, task: TaskParams) -> TaskResult:
        """KEDA path: enqueue task, poll for result blob."""
        import json as _json

        # Idempotency: skip enqueue if task is already in-flight
        lock_key = f"k8s_exec:{task.task_id}"
        existing_lock = await self._store.get(lock_key)
        if existing_lock is not None:
            try:
                expiry = int(existing_lock.split(":", 1)[1])
                if time.time() < expiry:
                    log.info("k8s_execution_already_enqueued", task_id=task.task_id)
                    return await self._poll_result(task)
            except (ValueError, IndexError):
                pass

        payload = _json.dumps(
            {
                "task_type": task.task_type,
                "task_id": task.task_id,
                "repo_url": task.repo_url,
                "branch": task.branch,
                "system_prompt": task.system_prompt,
                "user_prompt": task.user_prompt,
            }
        )
        await self._task_queue.enqueue(task.task_id, payload)
        lock_val = f"enqueued:{int(time.time()) + self._settings.k8s_job_timeout}"
        await self._store.set(lock_key, lock_val)

        return await self._poll_result(task)

    async def _poll_result(self, task: TaskParams) -> TaskResult:
        """Poll result blob until available or timeout."""
        deadline = asyncio.get_event_loop().time() + self._settings.k8s_job_timeout
        while asyncio.get_event_loop().time() < deadline:
            cached = await self._store.get(task.task_id)
            if cached is not None:
                return _parse_result(cached, task.task_type)
            await asyncio.sleep(_JOB_POLL_INTERVAL)
        msg = f"Task {task.task_id} timed out after {self._settings.k8s_job_timeout}s"
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
            if data["result_type"] == "error":
                summary = data.get("summary", "Task failed (unknown error)")
                log.error("task_error_result", summary=summary)
                return ReviewResult(summary=summary)
            return ReviewResult.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        pass
    if task_type == "coding":
        return CodingResult(summary=raw)
    return ReviewResult(summary=raw)
