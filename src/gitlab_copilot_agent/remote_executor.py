"""Remote task executor — claim-check dispatch for any KEDA-backed backend.

Dispatches tasks via Azure Storage Queue and polls for results in Blob Storage.
Works identically for K8s Jobs and ACA Job executions — the dispatch protocol
is the same. KEDA watches the queue and creates the appropriate backend
resource automatically.

Replaces the former k8s_executor.py and aca_executor.py (which were ~90%
duplicated). See architecture plan R2.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.git import tar_repo_to_bytes
from gitlab_copilot_agent.task_executor import (
    CodingResult,
    ReviewResult,
    TaskExecutionError,
    TaskResult,
)

if TYPE_CHECKING:
    from gitlab_copilot_agent.concurrency import ResultStore, TaskQueue
    from gitlab_copilot_agent.task_executor import TaskParams

log = structlog.get_logger()

_POLL_INTERVAL = 5  # Unified at 5s (was 2s for k8s, 5s for ACA) — more polite to storage
_LOCK_PREFIX = "remote_exec:"


def parse_result(raw: str, task_type: str) -> TaskResult:
    """Parse a raw result string into a structured TaskResult.

    If the string is valid JSON with a ``result_type`` field, parse it
    directly. Otherwise wrap the raw string as a summary.

    Args:
        raw: Raw result string from the result store.
        task_type: Expected task type ('review' or 'coding').

    Returns:
        Parsed TaskResult (ReviewResult or CodingResult).

    Raises:
        TaskExecutionError: If the result indicates an error.
    """
    try:
        data: object = json.loads(raw)
        if isinstance(data, dict) and "result_type" in data:
            result_type = str(data["result_type"])  # pyright: ignore[reportUnknownArgumentType]
            if result_type == "coding":
                return CodingResult.model_validate(data)
            if result_type == "error":
                summary = str(data.get("summary", "Task failed"))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                tb: str = str(data.get("traceback", ""))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                log.error("task_error_result", summary=summary, remote_traceback=tb)
                raise TaskExecutionError(summary)
            return ReviewResult.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        pass
    if task_type == "coding":
        return CodingResult(summary=raw)
    return ReviewResult(summary=raw)


class RemoteTaskExecutor:
    """Dispatches tasks via Azure Storage Queue (KEDA-triggered).

    Works identically for K8s Jobs and ACA Job executions — the
    dispatch protocol is the same. KEDA watches the queue and
    creates the appropriate backend resource automatically.

    Args:
        result_store: Blob-backed result store for task results.
        task_queue: Queue + blob store for claim-check dispatch.
        job_timeout: Maximum seconds to wait for task completion.
    """

    def __init__(
        self,
        result_store: ResultStore,
        task_queue: TaskQueue,
        job_timeout: int = 600,
    ) -> None:
        self._store = result_store
        self._task_queue = task_queue
        self._job_timeout = job_timeout

    async def execute(self, task: TaskParams) -> TaskResult:
        """Execute a task via claim-check dispatch.

        Args:
            task: Task parameters including repo path and prompts.

        Returns:
            Parsed task result from the remote runner.
        """
        bound = log.bind(task_id=task.task_id, task_type=task.task_type)

        cached = await self._store.get(task.task_id)
        if cached is not None:
            bound.info("remote_result_cached")
            return parse_result(cached, task.task_type)

        lock_key = f"{_LOCK_PREFIX}{task.task_id}"
        existing = await self._store.get(lock_key)
        if existing is not None and existing.startswith("enqueued:"):
            try:
                expiry = int(existing.split(":", 1)[1])
                if time.time() < expiry:
                    bound.info("remote_already_enqueued")
                    return await self._poll_result(task)
            except (ValueError, IndexError):
                pass

        repo_blob_key: str | None = None
        if task.repo_path:
            repo_blob_key = f"repos/{task.task_id}.tar.gz"
            tarball = await tar_repo_to_bytes(task.repo_path)
            await self._task_queue.upload_blob(repo_blob_key, tarball)

        payload = json.dumps(
            {
                "task_type": task.task_type,
                "task_id": task.task_id,
                "repo_blob_key": repo_blob_key,
                "system_prompt": task.system_prompt,
                "user_prompt": task.user_prompt,
                "plugins": task.plugins,
            }
        )
        bound.info("remote_enqueue_starting")
        await self._task_queue.enqueue(task.task_id, payload)
        lock_val = f"enqueued:{int(time.time()) + self._job_timeout}"
        await self._store.set(lock_key, lock_val)
        bound.info("remote_enqueue_complete")

        return await self._poll_result(task)

    async def _poll_result(self, task: TaskParams) -> TaskResult:
        """Poll ResultStore until result available or timeout."""
        bound = log.bind(task_id=task.task_id)
        deadline = asyncio.get_event_loop().time() + self._job_timeout
        polls = 0
        while asyncio.get_event_loop().time() < deadline:
            cached = await self._store.get(task.task_id)
            if cached is not None:
                bound.info("remote_result_found", polls=polls)
                return parse_result(cached, task.task_type)
            polls += 1
            await asyncio.sleep(_POLL_INTERVAL)
        msg = f"Task {task.task_id} timed out after {self._job_timeout}s"
        bound.error("remote_result_timeout", polls=polls)
        raise TimeoutError(msg)
