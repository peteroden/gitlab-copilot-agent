"""Shared Copilot SDK session runner — extracted from review_engine."""

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from typing import Any, cast

import structlog
from copilot import CopilotClient, SubprocessConfig
from copilot.session import (
    CustomAgentConfig,
    PermissionHandler,
    ProviderConfig,
    SystemMessageAppendConfig,
)

from gitlab_copilot_agent.config import Settings, TaskRunnerSettings
from gitlab_copilot_agent.metrics import (
    copilot_session_duration,
)
from gitlab_copilot_agent.process_sandbox import get_real_cli_path
from gitlab_copilot_agent.repo_config import discover_repo_config
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)

# Env vars safe to pass to the SDK subprocess.
_SDK_ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER"})


def build_sdk_env(github_token: str | None) -> dict[str, str]:
    """Build a minimal env dict for the SDK subprocess.

    Only allowed vars + GITHUB_TOKEN are passed. Service secrets
    (GITLAB_TOKEN, JIRA_*, WEBHOOK_SECRET) are excluded.
    """
    env = {k: v for k, v in os.environ.items() if k in _SDK_ENV_ALLOWLIST}
    if github_token:
        env["GITHUB_TOKEN"] = github_token
    return env


def _as_list(value: str | list[str]) -> list[str]:
    """Narrow a settings field (str | list[str] for pydantic-settings) to list[str]."""
    return value if isinstance(value, list) else []


def _merge_plugins(
    service_plugins: list[str],
    repo_plugins: list[str] | None,
) -> list[str]:
    """Merge service-level and repo-level plugins, preserving order and deduplicating."""
    seen: set[str] = set()
    result: list[str] = []
    for spec in [*service_plugins, *(repo_plugins or [])]:
        if spec not in seen:
            seen.add(spec)
            result.append(spec)
    return result


async def run_copilot_session(
    settings: Settings | TaskRunnerSettings,
    repo_path: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 300,
    task_type: str = "review",
    validate_response: Callable[[str], str | None] | None = None,
    plugins: list[str] | None = None,
) -> str:
    """Run a Copilot agent session and return the last assistant message.

    If *validate_response* is provided it is called with the assistant's first
    reply.  When it returns a non-None string that string is sent as a follow-up
    prompt **within the same session** (so the agent retains context) and the
    second reply is returned instead.  At most one follow-up is attempted.
    """
    session_start = time.monotonic()
    with _tracer.start_as_current_span(
        "copilot.session",
        attributes={
            "repo_path": repo_path,
            "timeout": timeout,
            "task_type": task_type,
        },
    ):
        cli_path = get_real_cli_path()
        try:
            # Per-session HOME isolation — plugins and SDK state never leak between sessions
            session_home = tempfile.mkdtemp(prefix="copilot-session-")
            try:
                # Merge service-level + repo-level plugins (deduplicated, order-preserved)
                effective_plugins = _merge_plugins(_as_list(settings.copilot_plugins), plugins)
                marketplaces = _as_list(settings.copilot_plugin_marketplaces)
                if effective_plugins or marketplaces:
                    from gitlab_copilot_agent.plugin_manager import setup_plugins

                    await setup_plugins(
                        session_home,
                        effective_plugins,
                        marketplaces or None,
                    )

                sdk_env = build_sdk_env(settings.github_token)
                sdk_env["HOME"] = session_home

                client = CopilotClient(
                    SubprocessConfig(
                        cli_path=cli_path,
                        env=sdk_env,
                        github_token=settings.github_token or None,
                    ),
                )
                await client.start()
                auth_status = await client.get_auth_status()
                auth_type = getattr(auth_status, "authType", None)
                is_authenticated = getattr(auth_status, "isAuthenticated", None)
                await log.ainfo(
                    "copilot_client_started",
                    cli_path=cli_path,
                    working_directory=repo_path,
                    task_type=task_type,
                    auth_type=auth_type,
                    is_authenticated=is_authenticated,
                    has_token=bool(settings.github_token),
                )
                if not is_authenticated and not settings.copilot_provider_type:
                    await log.aerror(
                        "copilot_auth_failed",
                        auth_type=auth_type,
                        has_token=bool(settings.github_token),
                        hint=(
                            "The GitHub token is missing or invalid. "
                            "Rotate the GITHUB_TOKEN secret with a PAT "
                            "that has the 'copilot' scope (classic) or "
                            "'Copilot requests: read' permission "
                            "(fine-grained)."
                        ),
                    )
                    raise RuntimeError(
                        "Copilot authentication failed: the configured "
                        "GITHUB_TOKEN is missing, expired, or lacks "
                        "required scopes. Rotate the token in Key Vault "
                        f"[auth_type={auth_type}]"
                    )

                try:
                    repo_config = discover_repo_config(repo_path)

                    system_content = system_prompt
                    if repo_config.instructions:
                        system_content += (
                            f"\n\n## Project-Specific Instructions\n\n{repo_config.instructions}\n"
                        )

                    system_msg: SystemMessageAppendConfig = {"content": system_content}

                    session_kwargs: dict[str, object] = {
                        "system_message": system_msg,
                        "working_directory": repo_path,
                        "on_permission_request": PermissionHandler.approve_all,
                    }

                    if repo_config.skill_directories:
                        session_kwargs["skill_directories"] = repo_config.skill_directories
                        await log.ainfo("skills_loaded", directories=repo_config.skill_directories)
                    if repo_config.custom_agents:
                        session_kwargs["custom_agents"] = [
                            cast(CustomAgentConfig, a.model_dump(exclude_none=True))
                            for a in repo_config.custom_agents
                        ]
                        await log.ainfo(
                            "agents_loaded",
                            agents=[a.name for a in repo_config.custom_agents],
                        )
                    if repo_config.instructions:
                        await log.ainfo("instructions_loaded")

                    if settings.copilot_provider_type:
                        provider: ProviderConfig = {
                            "type": cast(Any, settings.copilot_provider_type),
                        }
                        if settings.copilot_provider_base_url:
                            provider["base_url"] = settings.copilot_provider_base_url
                        if settings.copilot_provider_api_key:
                            provider["api_key"] = settings.copilot_provider_api_key
                        if settings.copilot_provider_type == "azure":
                            provider["azure"] = {"api_version": "2024-10-21"}
                        session_kwargs["provider"] = provider
                        session_kwargs["model"] = settings.copilot_model

                    session = await client.create_session(
                        **session_kwargs,  # type: ignore[arg-type]
                    )
                    try:
                        done = asyncio.Event()
                        messages: list[str] = []
                        session_error: dict[str, str] = {}

                        def on_event(event: Any) -> None:  # pyright: ignore[reportExplicitAny]
                            match getattr(event, "type", None):
                                case t if t and t.value == "assistant.message":
                                    content = getattr(event.data, "content", "")
                                    if content:
                                        messages.append(content)
                                case t if t and t.value == "session.error":
                                    data = getattr(event, "data", None)
                                    session_error["type"] = str(
                                        getattr(data, "error_type", "unknown")
                                    )
                                    session_error["message"] = str(getattr(data, "message", ""))
                                    done.set()
                                case t if t and t.value == "session.idle":
                                    done.set()
                                case _:
                                    pass

                        session.on(on_event)
                        await session.send(user_prompt)
                        await asyncio.wait_for(done.wait(), timeout=timeout)

                        if session_error:
                            await log.aerror(
                                "copilot_session_error",
                                error_type=session_error.get("type"),
                                error_message=session_error.get("message"),
                                task_type=task_type,
                                auth_type=auth_type,
                                is_authenticated=is_authenticated,
                            )
                            auth_info = f"[auth={auth_type}, ok={is_authenticated}]"
                            raise RuntimeError(
                                f"Copilot session error ({session_error.get('type')}): "
                                f"{session_error.get('message')} {auth_info}"
                            )

                        result = messages[-1] if messages else ""
                        await log.ainfo(
                            "copilot_session_first_result",
                            message_count=len(messages),
                            result_length=len(result),
                            result_empty=not result,
                            task_type=task_type,
                        )

                        if validate_response is not None:
                            follow_up = validate_response(result)
                            if follow_up:
                                await log.ainfo(
                                    "copilot_session_retry", reason="validate_response"
                                )
                                done.clear()
                                messages.clear()
                                session_error.clear()
                                await session.send(follow_up)
                                await asyncio.wait_for(done.wait(), timeout=timeout)

                                if session_error:
                                    await log.aerror(
                                        "copilot_session_retry_error",
                                        error_type=session_error.get("type"),
                                        error_message=session_error.get("message"),
                                        task_type=task_type,
                                        auth_type=auth_type,
                                        is_authenticated=is_authenticated,
                                    )
                                    auth_info = f"[auth={auth_type}, ok={is_authenticated}]"
                                    raise RuntimeError(
                                        f"Copilot session error on retry "
                                        f"({session_error.get('type')}): "
                                        f"{session_error.get('message')} {auth_info}"
                                    )

                                result = messages[-1] if messages else result
                                await log.ainfo(
                                    "copilot_session_retry_result",
                                    message_count=len(messages),
                                    result_length=len(result),
                                    result_empty=not result,
                                )
                    finally:
                        await session.disconnect()
                finally:
                    await client.stop()
                return result
            finally:
                shutil.rmtree(session_home, ignore_errors=True)
        finally:
            elapsed = time.monotonic() - session_start
            copilot_session_duration.record(elapsed, {"task_type": task_type})
