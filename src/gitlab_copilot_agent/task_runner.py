"""Task runner entrypoint for k8s Job pods.

Executes a single Copilot task (review or coding) and exits.
Designed to run as: python -m gitlab_copilot_agent.task_runner
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import structlog

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.copilot_session import run_copilot_session
from gitlab_copilot_agent.git_operations import git_clone

log = structlog.get_logger()

# Required env vars for task runner
TASK_TYPE_VAR = "TASK_TYPE"
TASK_ID_VAR = "TASK_ID"
REPO_URL_VAR = "REPO_URL"
BRANCH_VAR = "BRANCH"
TASK_PAYLOAD_VAR = "TASK_PAYLOAD"
RESULT_BACKEND_VAR = "RESULT_BACKEND"  # "stdout" (default) or "redis"
REDIS_URL_VAR = "REDIS_URL"

VALID_TASK_TYPES = frozenset({"review", "coding"})


def _get_required_env(name: str) -> str:
    """Get a required environment variable or raise."""
    value = os.environ.get(name)
    if not value:
        msg = f"Required environment variable {name} is not set"
        raise RuntimeError(msg)
    return value


def _parse_task_payload(raw: str) -> dict[str, str]:
    """Parse TASK_PAYLOAD JSON into dict."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in {TASK_PAYLOAD_VAR}: {e}"
        raise RuntimeError(msg) from e
    if not isinstance(payload, dict):
        msg = f"{TASK_PAYLOAD_VAR} must be a JSON object"
        raise RuntimeError(msg)
    return payload


async def _write_result(task_id: str, result: str, backend: str, redis_url: str | None) -> None:
    """Write task result to configured backend."""
    if backend == "redis":
        if not redis_url:
            msg = f"{REDIS_URL_VAR} required when {RESULT_BACKEND_VAR}=redis"
            raise RuntimeError(msg)
        try:
            import redis.asyncio as aioredis  # type: ignore[import-untyped]
        except ImportError as e:
            msg = "redis package not installed (required for redis backend)"
            raise RuntimeError(msg) from e

        client = aioredis.from_url(redis_url)
        try:
            await client.set(f"task:{task_id}:result", result, ex=3600)
            await log.ainfo("result_written", backend="redis", task_id=task_id)
        finally:
            await client.aclose()
    else:
        # stdout backend — print result as JSON
        print(json.dumps({"task_id": task_id, "result": result}))
        await log.ainfo("result_written", backend="stdout", task_id=task_id)


async def run_task() -> int:
    """Execute a single task and return exit code (0=success, 1=failure)."""
    try:
        task_type = _get_required_env(TASK_TYPE_VAR)
        task_id = _get_required_env(TASK_ID_VAR)
        repo_url = _get_required_env(REPO_URL_VAR)
        branch = _get_required_env(BRANCH_VAR)
        payload_raw = _get_required_env(TASK_PAYLOAD_VAR)

        result_backend = os.environ.get(RESULT_BACKEND_VAR, "stdout")
        redis_url = os.environ.get(REDIS_URL_VAR)
    except RuntimeError:
        await log.aexception("task_env_error")
        return 1

    bound_log = log.bind(task_id=task_id, task_type=task_type)

    if task_type not in VALID_TASK_TYPES:
        await bound_log.aerror("invalid_task_type", valid=list(VALID_TASK_TYPES))
        return 1

    try:
        payload = _parse_task_payload(payload_raw)
    except RuntimeError:
        await bound_log.aexception("payload_parse_error")
        return 1

    system_prompt = payload.get("system_prompt", "")
    user_prompt = payload.get("user_prompt", "")

    if not system_prompt or not user_prompt:
        await bound_log.aerror(
            "missing_prompts", has_system=bool(system_prompt), has_user=bool(user_prompt)
        )
        return 1

    await bound_log.ainfo("task_started", repo_url=repo_url, branch=branch)

    settings = Settings()
    repo_path: Path | None = None
    try:
        repo_path = await git_clone(
            repo_url, branch, settings.gitlab_token, clone_dir=settings.clone_dir
        )
        result = await run_copilot_session(
            settings=settings,
            repo_path=str(repo_path),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            task_type=task_type,
        )
        await _write_result(task_id, result, result_backend, redis_url)
        await bound_log.ainfo("task_completed")
        return 0
    except Exception:
        await bound_log.aexception("task_failed")
        return 1
    finally:
        if repo_path and repo_path.exists():
            shutil.rmtree(repo_path, ignore_errors=True)


def main() -> None:
    """Entrypoint for python -m gitlab_copilot_agent.task_runner."""
    exit_code = asyncio.run(run_task())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
