"""Task runner entrypoint for k8s Job pods — ``python -m gitlab_copilot_agent.task_runner``."""

import asyncio
import json
import os
import shutil
import sys
from urllib.parse import ParseResult, urlparse

import structlog

from gitlab_copilot_agent.coding_engine import CODING_SYSTEM_PROMPT
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.copilot_session import run_copilot_session
from gitlab_copilot_agent.git_operations import _sanitize_url_for_log as _sanitize_url
from gitlab_copilot_agent.git_operations import git_clone
from gitlab_copilot_agent.review_engine import SYSTEM_PROMPT as REVIEW_SYSTEM_PROMPT

log = structlog.get_logger()
ENV_TASK_TYPE, ENV_TASK_ID, ENV_REPO_URL = "TASK_TYPE", "TASK_ID", "REPO_URL"
ENV_BRANCH, ENV_TASK_PAYLOAD = "BRANCH", "TASK_PAYLOAD"
ENV_REDIS_URL = "REDIS_URL"
VALID_TASK_TYPES: frozenset[str] = frozenset({"review", "coding", "echo"})
_RESULT_KEY_PREFIX = "result:"
_RESULT_TTL = 3600  # 1 hour


async def _store_result(task_id: str, result: str) -> None:
    """Persist result to Redis if REDIS_URL is set."""
    redis_url = os.environ.get(ENV_REDIS_URL, "").strip()
    if not redis_url:
        return
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url)
    try:
        await client.set(f"{_RESULT_KEY_PREFIX}{task_id}", result, ex=_RESULT_TTL)
    finally:
        await client.aclose()


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


async def run_task() -> int:
    try:
        task_type = _get_required_env(ENV_TASK_TYPE)
        task_id = _get_required_env(ENV_TASK_ID)
        repo_url = _get_required_env(ENV_REPO_URL)
        branch = _get_required_env(ENV_BRANCH)
        payload_raw = _get_required_env(ENV_TASK_PAYLOAD)
    except RuntimeError:
        await log.aerror("missing_env_var", exc_info=True)
        return 1
    bound_log = log.bind(task_id=task_id, task_type=task_type)
    if task_type not in VALID_TASK_TYPES:
        await bound_log.aerror("invalid_task_type", valid=sorted(VALID_TASK_TYPES))
        return 1
    if task_type == "echo":
        try:
            user_prompt = _parse_task_payload(payload_raw).get("prompt", payload_raw)
            result = json.dumps({"echo": user_prompt, "task_id": task_id})
            await _store_result(task_id, result)
            await bound_log.ainfo("echo_complete")
            return 0
        except Exception:
            await bound_log.aerror("echo_failed", exc_info=True)
            return 1
    settings = Settings()
    _validate_repo_url(repo_url, settings.gitlab_url)
    await bound_log.ainfo("task_start", repo=_sanitize_url(repo_url), branch=branch)
    user_prompt = _parse_task_payload(payload_raw).get("prompt", payload_raw)
    repo_path = await git_clone(
        repo_url, branch, settings.gitlab_token, clone_dir=settings.clone_dir
    )
    try:
        prompt = REVIEW_SYSTEM_PROMPT if task_type == "review" else CODING_SYSTEM_PROMPT
        result = await run_copilot_session(
            settings, str(repo_path), prompt, user_prompt, task_type=task_type
        )
        await _store_result(task_id, result)
        print(json.dumps({"task_id": task_id, "result": result}), flush=True)  # noqa: T201
        await bound_log.ainfo("task_complete")
        return 0
    except Exception:
        await bound_log.aerror("task_failed", exc_info=True)
        return 1
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


def main() -> None:
    sys.exit(asyncio.run(run_task()))


if __name__ == "__main__":
    main()
