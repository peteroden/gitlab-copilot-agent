"""Task runner entrypoint for k8s Job pods — ``python -m gitlab_copilot_agent.task_runner``."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import ParseResult, urlparse

import structlog

from gitlab_copilot_agent.coding_engine import parse_agent_output
from gitlab_copilot_agent.concurrency import QueueMessage, TaskQueue
from gitlab_copilot_agent.config import TaskRunnerSettings
from gitlab_copilot_agent.copilot_session import run_copilot_session
from gitlab_copilot_agent.git_operations import (
    MAX_PATCH_SIZE,
    git_clone,
    git_diff_staged,
    git_head_sha,
)
from gitlab_copilot_agent.git_operations import _sanitize_url_for_log as _sanitize_url
from gitlab_copilot_agent.prompt_defaults import get_prompt

log = structlog.get_logger()
ENV_TASK_TYPE, ENV_TASK_ID, ENV_REPO_URL = "TASK_TYPE", "TASK_ID", "REPO_URL"
ENV_BRANCH, ENV_TASK_PAYLOAD = "BRANCH", "TASK_PAYLOAD"
VALID_TASK_TYPES: frozenset[str] = frozenset({"review", "coding", "echo"})
_RESULT_TTL = 3600  # 1 hour

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


async def _dequeue_task() -> tuple[dict[str, str], QueueMessage, TaskQueue] | None:
    """Dequeue a task from Azure Storage Queue (if configured).

    Creates TaskRunnerSettings internally; returns None if settings
    can't be created or dispatch_backend isn't azure_storage.
    """
    try:
        settings = TaskRunnerSettings()
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
    msg = await queue.dequeue(visibility_timeout=600)
    if msg is None:
        await log.ainfo("dequeue_empty")
        await queue.aclose()
        return None

    params: dict[str, str] = json.loads(msg.payload)
    return params, msg, queue


def _get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set or empty")
    return value


def _parse_task_payload(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {ENV_TASK_PAYLOAD}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{ENV_TASK_PAYLOAD} must be a JSON object, got {type(data).__name__}")
    return data


def _effective_port(parsed: ParseResult) -> int:
    """Return explicit port or default for scheme (443 for https, 80 for http)."""
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _validate_repo_url(repo_url: str, gitlab_url: str) -> None:
    repo_parsed, gitlab_parsed = urlparse(repo_url), urlparse(gitlab_url)
    repo_host, gitlab_host = repo_parsed.hostname, gitlab_parsed.hostname
    if not repo_host or not gitlab_host:
        raise RuntimeError("REPO_URL or GITLAB_URL has no host component")
    if repo_host.lower() != gitlab_host.lower() or _effective_port(repo_parsed) != _effective_port(
        gitlab_parsed
    ):
        raise RuntimeError(
            f"REPO_URL authority does not match GITLAB_URL "
            f"({repo_host}:{_effective_port(repo_parsed)} vs "
            f"{gitlab_host}:{_effective_port(gitlab_parsed)})"
        )


async def _build_coding_result(
    repo_path: Path, summary: str, bound_log: structlog.stdlib.BoundLogger
) -> str:
    """Stage explicitly listed files, capture diff and base SHA."""
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

    base_sha = await git_head_sha(repo_path)
    patch = await git_diff_staged(repo_path)
    if len(patch.encode()) > MAX_PATCH_SIZE:
        raise RuntimeError(f"Patch size {len(patch.encode())} exceeds limit {MAX_PATCH_SIZE}")
    await bound_log.ainfo("diff_captured", patch_bytes=len(patch.encode()), base_sha=base_sha[:12])
    return json.dumps(
        {
            "result_type": "coding",
            "summary": agent_output.summary,
            "patch": patch,
            "base_sha": base_sha,
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
        params_dict, queue_msg, task_queue = queue_result
        task_type = params_dict["task_type"]
        task_id = params_dict["task_id"]
        repo_url = params_dict["repo_url"]
        branch = params_dict["branch"]
        user_prompt = params_dict["user_prompt"]
        payload_raw = json.dumps({"prompt": user_prompt})
    else:
        try:
            task_type = _get_required_env(ENV_TASK_TYPE)
            task_id = _get_required_env(ENV_TASK_ID)
            repo_url = _get_required_env(ENV_REPO_URL)
            branch = _get_required_env(ENV_BRANCH)
            payload_raw = _get_required_env(ENV_TASK_PAYLOAD)
            user_prompt = _parse_task_payload(payload_raw).get("prompt", payload_raw)
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
            settings = TaskRunnerSettings() if queue_result is not None else None
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

    settings = TaskRunnerSettings()
    _validate_repo_url(repo_url, settings.gitlab_url)
    await bound_log.ainfo("task_start", repo=_sanitize_url(repo_url), branch=branch)
    repo_path = await git_clone(
        repo_url, branch, settings.gitlab_token, clone_dir=settings.clone_dir
    )
    try:
        if task_type == "coding":
            from gitlab_copilot_agent.coding_engine import ensure_git_exclude

            ensure_git_exclude(str(repo_path))
        prompt = get_prompt(settings, "review" if task_type == "review" else "coding")
        summary = await run_copilot_session(
            settings,
            str(repo_path),
            prompt,
            user_prompt,
            task_type=task_type,
            validate_response=_coding_response_validator if task_type == "coding" else None,
        )
        if task_type == "coding":
            result = await _build_coding_result(repo_path, summary, bound_log)
        else:
            result = json.dumps({"result_type": "review", "summary": summary})
        await _store_result(task_id, result, settings)
        if task_queue and queue_msg:
            await task_queue.complete(queue_msg)
        print(json.dumps({"task_id": task_id, "result": result}), flush=True)  # noqa: T201
        await bound_log.ainfo("task_complete")
        return 0
    except Exception:
        await bound_log.aerror("task_failed", exc_info=True)
        return 1
    finally:
        if task_queue:
            await task_queue.aclose()
        shutil.rmtree(repo_path, ignore_errors=True)


def main() -> None:
    sys.exit(asyncio.run(run_task()))


if __name__ == "__main__":
    main()
