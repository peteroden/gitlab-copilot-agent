"""Shared Copilot SDK session runner — extracted from review_engine."""

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from typing import Any, cast

import structlog
from copilot import CopilotClient
from copilot.types import (
    CopilotClientOptions,
    CustomAgentConfig,
    PermissionHandler,
    ProviderConfig,
    SessionConfig,
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

                client_opts: CopilotClientOptions = {
                    "cli_path": cli_path,
                    "env": sdk_env,
                }
                if settings.github_token:
                    client_opts["github_token"] = settings.github_token

                client = CopilotClient(client_opts)
                await client.start()

                try:
                    repo_config = discover_repo_config(repo_path)

                    system_content = system_prompt
                    if repo_config.instructions:
                        system_content += (
                            f"\n\n## Project-Specific Instructions\n\n{repo_config.instructions}\n"
                        )

                    session_opts: SessionConfig = {
                        "system_message": {"content": system_content},
                        "working_directory": repo_path,
                    }

                    if repo_config.skill_directories:
                        session_opts["skill_directories"] = repo_config.skill_directories
                        await log.ainfo("skills_loaded", directories=repo_config.skill_directories)
                    if repo_config.custom_agents:
                        session_opts["custom_agents"] = [
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
                        session_opts["provider"] = provider
                        session_opts["model"] = settings.copilot_model

                    session_opts["on_permission_request"] = PermissionHandler.approve_all
                    session = await client.create_session(session_opts)
                    try:
                        done = asyncio.Event()
                        messages: list[str] = []

                        def on_event(event: Any) -> None:  # pyright: ignore[reportExplicitAny]
                            match getattr(event, "type", None):
                                case t if t and t.value == "assistant.message":
                                    content = getattr(event.data, "content", "")
                                    if content:
                                        messages.append(content)
                                case t if t and t.value == "session.idle":
                                    done.set()
                                case _:
                                    pass

                        session.on(on_event)
                        await session.send({"prompt": user_prompt})
                        await asyncio.wait_for(done.wait(), timeout=timeout)

                        result = messages[-1] if messages else ""

                        if validate_response is not None:
                            follow_up = validate_response(result)
                            if follow_up:
                                await log.ainfo(
                                    "copilot_session_retry", reason="validate_response"
                                )
                                done.clear()
                                messages.clear()
                                await session.send({"prompt": follow_up})
                                await asyncio.wait_for(done.wait(), timeout=timeout)
                                result = messages[-1] if messages else result
                    finally:
                        await session.destroy()
                finally:
                    await client.stop()
                return result
            finally:
                shutil.rmtree(session_home, ignore_errors=True)
        finally:
            elapsed = time.monotonic() - session_start
            copilot_session_duration.record(elapsed, {"task_type": task_type})
