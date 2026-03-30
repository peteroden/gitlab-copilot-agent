"""Task runner entrypoint for k8s Job pods — ``python -m gitlab_copilot_agent.task_runner``."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.coding_engine import parse_agent_output
from gitlab_copilot_agent.concurrency import QueueMessage, TaskQueue
from gitlab_copilot_agent.config import TaskRunnerSettings
from gitlab_copilot_agent.copilot_session import run_copilot_session
from gitlab_copilot_agent.git_operations import (
    MAX_PATCH_SIZE,
    extract_repo_tarball,
    git_diff_staged,
    git_head_sha,
)
from gitlab_copilot_agent.prompt_defaults import get_prompt

log = structlog.get_logger()
ENV_TASK_TYPE, ENV_TASK_ID = "TASK_TYPE", "TASK_ID"
ENV_TASK_PAYLOAD = "TASK_PAYLOAD"
VALID_TASK_TYPES: frozenset[str] = frozenset({"review", "coding", "echo"})
_RESULT_TTL = 3600  # 1 hour


class QueueTaskPayload(BaseModel):
    """Typed representation of a task dequeued from Azure Storage Queue."""

    model_config = ConfigDict(strict=True)

    task_type: str = Field(description="Task type: review, coding, or echo")
    task_id: str = Field(description="Unique task identifier for idempotency")
    repo_blob_key: str | None = Field(default=None, description="Blob key for repo tarball")
    system_prompt: str = Field(default="", description="System prompt for Copilot session")
    user_prompt: str = Field(description="User prompt for Copilot session")
    plugins: list[str] | None = Field(default=None, description="Per-repo plugin specs")


_RETRY_PROMPT = (
    "Your response did not include the required JSON output block. "
    "Please output ONLY a fenced JSON block with your summary and the "
    "list of files you intentionally changed:\n\n"
    "```json\n"
    '{"summary": "Brief description of changes", "files_changed": ["path/to/file.py"]}\n'
    "```"
)


def _coding_response_validator(response: str) -> str | None:
    """Return a follow-up prompt if the coding response is missing structured output."""
    if parse_agent_output(response) is not None:
        return None
    return _RETRY_PROMPT


async def _store_result(
    task_id: str, result: str, settings: TaskRunnerSettings | None = None
) -> None:
    """Persist result to the configured ResultStore (Azure Storage Blob)."""
    if settings is None:
        return
    from gitlab_copilot_agent.state import create_result_store

    store = create_result_store(
        azure_storage_account_url=settings.azure_storage_account_url,
        azure_storage_connection_string=settings.azure_storage_connection_string,
        task_blob_container=settings.task_blob_container,
    )
    try:
        await store.set(task_id, result)
    finally:
        await store.aclose()


async def _dequeue_task() -> tuple[QueueTaskPayload, QueueMessage, TaskQueue] | None:
    """Dequeue a task from Azure Storage Queue (if configured).

    Creates TaskRunnerSettings internally; returns None if settings
    can't be created or dispatch_backend isn't azure_storage.
    """
    try:
        settings = TaskRunnerSettings()  # pyright: ignore[reportCallIssue]
    except Exception:
        await log.awarning("dequeue_settings_failed", exc_info=True)
        return None
    if not settings.azure_storage_connection_string and (
        not settings.azure_storage_queue_url or not settings.azure_storage_account_url
    ):
        await log.awarning("dequeue_no_azure_config")
        return None

    from gitlab_copilot_agent.state import create_task_queue

    queue = create_task_queue(
        azure_storage_queue_url=settings.azure_storage_queue_url,
        azure_storage_account_url=settings.azure_storage_account_url,
        azure_storage_connection_string=settings.azure_storage_connection_string,
        task_queue_name=settings.task_queue_name,
        task_blob_container=settings.task_blob_container,
    )
    msg = await queue.dequeue(
        visibility_timeout=settings.k8s_job_timeout + settings.queue_visibility_buffer,
    )
    if msg is None:
        await log.ainfo("dequeue_empty")
        await queue.aclose()
        return None

    payload = QueueTaskPayload.model_validate_json(msg.payload)
    return payload, msg, queue


def _get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set or empty")
    return value


def _parse_task_payload(raw: str) -> dict[str, object]:
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {ENV_TASK_PAYLOAD}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{ENV_TASK_PAYLOAD} must be a JSON object, got {type(data).__name__}")
    return {str(k): v for k, v in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]


async def _build_coding_result(
    repo_path: Path,
    summary: str,
    bound_log: structlog.stdlib.BoundLogger,
    pre_session_sha: str,
) -> str:
    """Stage explicitly listed files, capture diff against pre-session HEAD."""
    agent_output = parse_agent_output(summary)
    if not agent_output or not agent_output.files_changed:
        raise RuntimeError(
            "Agent did not return a valid files_changed list. "
            "Cannot determine which files to commit."
        )

    for f in agent_output.files_changed:
        if ".." in f.split("/"):
            await bound_log.awarning("path_traversal_skipped", file=f)
            continue
        await _run_git_simple(repo_path, "add", "--", f)
    await bound_log.ainfo("staged_explicit_files", count=len(agent_output.files_changed))

    # Capture staged diff first. If empty, the agent may have already
    # committed — fall back to diffing against the pre-session HEAD.
    patch = await git_diff_staged(repo_path)
    if not patch:
        current_sha = await git_head_sha(repo_path)
        if current_sha != pre_session_sha:
            await bound_log.ainfo(
                "agent_committed_detected",
                pre_sha=pre_session_sha[:12],
                current_sha=current_sha[:12],
            )
            patch = await _run_git_simple(repo_path, "diff", pre_session_sha, "HEAD", "--binary")

    if len(patch.encode()) > MAX_PATCH_SIZE:
        raise RuntimeError(f"Patch size {len(patch.encode())} exceeds limit {MAX_PATCH_SIZE}")
    await bound_log.ainfo(
        "diff_captured", patch_bytes=len(patch.encode()), base_sha=pre_session_sha[:12]
    )
    return json.dumps(
        {
            "result_type": "coding",
            "summary": agent_output.summary,
            "patch": patch,
            "base_sha": pre_session_sha,
        }
    )


async def _run_git_simple(repo_path: Path, *args: str) -> str:
    """Run a simple git command in the task runner pod."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_path),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {stderr.decode().strip()}")
    return stdout.decode().strip()


async def run_task() -> int:  # noqa: C901 — dispatch routing requires branching
    # Priority: Azure Storage Queue → env vars (legacy K8s)
    queue_msg: QueueMessage | None = None
    task_queue: TaskQueue | None = None

    queue_result = await _dequeue_task()
    if queue_result is not None:
        payload, queue_msg, task_queue = queue_result
        task_type = payload.task_type
        task_id = payload.task_id
        repo_blob_key = payload.repo_blob_key
        user_prompt = payload.user_prompt
        plugins = payload.plugins
    else:
        try:
            task_type = _get_required_env(ENV_TASK_TYPE)
            task_id = _get_required_env(ENV_TASK_ID)
            payload_raw = _get_required_env(ENV_TASK_PAYLOAD)
            user_prompt = str(_parse_task_payload(payload_raw).get("prompt", payload_raw))
            repo_blob_key = None
            plugins = None
        except RuntimeError as exc:
            await log.aerror("missing_env_var", error=str(exc))
            return 1

    bound_log = log.bind(task_id=task_id, task_type=task_type)
    if task_type not in VALID_TASK_TYPES:
        await bound_log.aerror("invalid_task_type", valid=sorted(VALID_TASK_TYPES))
        return 1
    if task_type == "echo":
        try:
            result = json.dumps({"echo": user_prompt, "task_id": task_id})
            settings = TaskRunnerSettings() if queue_result is not None else None  # pyright: ignore[reportCallIssue]
            await _store_result(task_id, result, settings)
            if task_queue and queue_msg:
                await task_queue.complete(queue_msg)
            await bound_log.ainfo("echo_complete")
            return 0
        except Exception:
            await bound_log.aerror("echo_failed", exc_info=True)
            return 1
        finally:
            if task_queue:
                await task_queue.aclose()

    # Review/coding tasks require blob-based repo transfer
    settings = TaskRunnerSettings()  # pyright: ignore[reportCallIssue]
    repo_path: Path | None = None
    try:
        if not repo_blob_key or task_queue is None:
            await bound_log.aerror(
                "repo_blob_required",
                detail="Review/coding tasks require queue-based dispatch with repo_blob_key",
            )
            return 1
        if not repo_blob_key.startswith("repos/"):
            await bound_log.aerror("invalid_repo_blob_key", key=repo_blob_key)
            return 1

        await bound_log.ainfo("task_start", repo_blob_key=repo_blob_key)
        tarball = await task_queue.download_blob(repo_blob_key)
        repo_path = await extract_repo_tarball(tarball, settings.clone_dir)
        pre_session_sha: str | None = None
        if task_type == "coding":
            from gitlab_copilot_agent.coding_engine import ensure_git_exclude

            ensure_git_exclude(str(repo_path))
            pre_session_sha = await git_head_sha(repo_path)
        prompt = get_prompt(settings, "review" if task_type == "review" else "coding")
        summary = await run_copilot_session(
            settings,
            str(repo_path),
            prompt,
            user_prompt,
            task_type=task_type,
            validate_response=_coding_response_validator if task_type == "coding" else None,
            plugins=plugins,
        )
        if task_type == "coding":
            assert pre_session_sha is not None
            result = await _build_coding_result(repo_path, summary, bound_log, pre_session_sha)
        else:
            result = json.dumps({"result_type": "review", "summary": summary})
        await _store_result(task_id, result, settings)
        if task_queue and queue_msg:
            await task_queue.complete(queue_msg)
        print(json.dumps({"task_id": task_id, "result": result}), flush=True)  # noqa: T201
        await bound_log.ainfo("task_complete")
        return 0
    except Exception as exc:
        import traceback

        await bound_log.aerror("task_failed", error=str(exc), traceback=traceback.format_exc())
        error_result = json.dumps(
            {"result_type": "error", "error": True, "summary": f"Task failed: {exc}"}
        )
        try:
            await _store_result(task_id, error_result, settings)
            if task_queue and queue_msg:
                await task_queue.complete(queue_msg)
        except Exception:
            await bound_log.awarning("error_result_write_failed", exc_info=True)
        return 1
    finally:
        if task_queue:
            await task_queue.aclose()
        if repo_path is not None:
            shutil.rmtree(repo_path, ignore_errors=True)


def main() -> None:
    sys.exit(asyncio.run(run_task()))


if __name__ == "__main__":
    main()
