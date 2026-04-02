"""ContainerAppsTaskExecutor — dispatches tasks via Azure Storage Queue (KEDA-triggered)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.git_operations import tar_repo_to_bytes
from gitlab_copilot_agent.task_executor import (
    CodingResult,
    ReviewResult,
    TaskExecutionError,
    TaskResult,
)

if TYPE_CHECKING:
    from gitlab_copilot_agent.concurrency import ResultStore, TaskQueue
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.task_executor import TaskParams

log = structlog.get_logger()

_JOB_POLL_INTERVAL = 5  # seconds between result blob checks
_EXECUTION_LOCK_TTL = 900  # sentinel TTL to prevent duplicate enqueues
_EXECUTION_LOCK_PREFIX = "aca_exec:"


def _build_dispatch_payload(task: TaskParams, repo_blob_key: str | None) -> str:
    """Serialize task params for the queue (Claim Check blob payload)."""
    return json.dumps(
        {
            "task_type": task.task_type,
            "task_id": task.task_id,
            "repo_blob_key": repo_blob_key,
            "system_prompt": task.system_prompt,
            "user_prompt": task.user_prompt,
            "plugins": task.plugins,
        }
    )


def _parse_result(raw: str, task_type: str) -> TaskResult:
    """Parse a raw result string into a structured TaskResult."""
    try:
        data: object = json.loads(raw)
        if isinstance(data, dict) and "result_type" in data:
            if data["result_type"] == "coding":
                return CodingResult.model_validate(data)
            if data["result_type"] == "error":
                summary = str(data.get("summary", "Task failed (unknown error)"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                tb = data.get("traceback", "")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                log.error("task_error_result", summary=summary, remote_traceback=tb)
                raise TaskExecutionError(summary)
            return ReviewResult.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        pass
    if task_type == "coding":
        return CodingResult(summary=raw)
    return ReviewResult(summary=raw)


class ContainerAppsTaskExecutor:
    """Dispatches tasks via Azure Storage Queue; KEDA triggers job executions.

    The controller enqueues a task (Claim Check: params blob + queue message)
    and polls the result blob.  KEDA watches the queue and creates ACA Job
    executions automatically — no ARM API calls needed.
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
        bound = log.bind(task_id=task.task_id, task_type=task.task_type)
        cached = await self._store.get(task.task_id)
        if cached is not None:
            bound.info("aca_result_cached")
            return _parse_result(cached, task.task_type)

        # Idempotency: if another call already enqueued this task, just poll
        lock_key = f"{_EXECUTION_LOCK_PREFIX}{task.task_id}"
        existing_lock = await self._store.get(lock_key)
        if existing_lock is not None and existing_lock.startswith("enqueued:"):
            try:
                expiry = int(existing_lock.split(":", 1)[1])
                if time.time() < expiry:
                    bound.info("aca_execution_already_enqueued")
                    return await self._poll_blob_result(task)
            except (ValueError, IndexError):
                pass  # malformed lock, proceed to re-enqueue

        # Upload repo tarball to blob storage (replaces git clone on runner)
        repo_blob_key: str | None = None
        if task.repo_path:
            repo_blob_key = f"repos/{task.task_id}.tar.gz"
            tarball = await tar_repo_to_bytes(task.repo_path)
            await self._task_queue.upload_blob(repo_blob_key, tarball)

        payload = _build_dispatch_payload(task, repo_blob_key)
        bound.info("aca_enqueue_starting")
        await self._task_queue.enqueue(task.task_id, payload)
        lock_val = f"enqueued:{int(time.time()) + _EXECUTION_LOCK_TTL}"
        await self._store.set(lock_key, lock_val)
        bound.info("aca_enqueue_complete")

        # KEDA watches the queue and triggers job execution automatically
        return await self._poll_blob_result(task)

    async def _poll_blob_result(self, task: TaskParams) -> TaskResult:
        """Poll ResultStore for result blob (KEDA handles job lifecycle)."""
        deadline = asyncio.get_event_loop().time() + self._settings.aca_job_timeout
        while asyncio.get_event_loop().time() < deadline:
            cached = await self._store.get(task.task_id)
            if cached is not None:
                return _parse_result(cached, task.task_type)
            await asyncio.sleep(_JOB_POLL_INTERVAL)
        msg = f"Task {task.task_id} timed out after {self._settings.aca_job_timeout}s"
        raise TimeoutError(msg)
