"""Shared Copilot SDK session runner — extracted from review_engine."""

import asyncio
import os
import time
from typing import Any, cast

import structlog
from copilot import CopilotClient
from copilot.types import (
    CopilotClientOptions,
    CustomAgentConfig,
    ProviderConfig,
    SessionConfig,
)

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.metrics import (
    copilot_session_duration,
    sandbox_active,
    sandbox_duration,
    sandbox_outcome_total,
)
from gitlab_copilot_agent.process_sandbox import get_sandbox
from gitlab_copilot_agent.repo_config import discover_repo_config
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)

# Env vars safe to pass to the SDK subprocess.
_SDK_ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER"})

# Extra vars forwarded only when sandbox_method=docker (DinD needs daemon access).
_DOCKER_ENV_VARS = frozenset({"DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"})


def build_sdk_env(github_token: str | None, *, sandbox_method: str = "bwrap") -> dict[str, str]:
    """Build a minimal env dict for the SDK subprocess.

    Only allowed vars + GITHUB_TOKEN are passed. Service secrets
    (GITLAB_TOKEN, JIRA_*, WEBHOOK_SECRET) are excluded.
    DOCKER_* vars are only forwarded when sandbox_method is 'docker'.
    """
    extra = _DOCKER_ENV_VARS if sandbox_method == "docker" else frozenset()
    env = {k: v for k, v in os.environ.items() if k in _SDK_ENV_ALLOWLIST | extra}
    if github_token:
        env["GITHUB_TOKEN"] = github_token
    return env


async def run_copilot_session(
    settings: Settings,
    repo_path: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 300,
    task_type: str = "review",
) -> str:
    """Run a Copilot agent session and return the last assistant message."""
    sandbox_start = time.monotonic()
    outcome = "error"
    method = settings.sandbox_method
    with _tracer.start_as_current_span(
        "copilot.session",
        attributes={
            "repo_path": repo_path,
            "timeout": timeout,
            "sandbox.method": method,
            "task_type": task_type,
        },
    ):
        sandbox = get_sandbox(settings)
        cli_wrapper = sandbox.create_cli_wrapper(repo_path)
        sandbox_active.add(1)
        try:
            client_opts: CopilotClientOptions = {
                "cli_path": cli_wrapper,
                "env": build_sdk_env(
                    settings.github_token, sandbox_method=settings.sandbox_method
                ),
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
                        cast(CustomAgentConfig, a) for a in repo_config.custom_agents
                    ]
                    await log.ainfo(
                        "agents_loaded",
                        agents=[a["name"] for a in repo_config.custom_agents],
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

                session = await client.create_session(session_opts)
                try:
                    done = asyncio.Event()
                    messages: list[str] = []

                    def on_event(event: Any) -> None:
                        match getattr(event, "type", None):
                            case t if t and t.value == "assistant.message":
                                content = getattr(event.data, "content", "")
                                if content:
                                    messages.append(content)
                            case t if t and t.value == "session.idle":
                                done.set()

                    session.on(on_event)
                    await session.send({"prompt": user_prompt})
                    await asyncio.wait_for(done.wait(), timeout=timeout)
                finally:
                    await session.destroy()

                result = messages[-1] if messages else ""
            finally:
                await client.stop()
            outcome = "success"
            return result
        except Exception:
            raise
        finally:
            sandbox.cleanup()
            sandbox_active.add(-1)
            elapsed = time.monotonic() - sandbox_start
            labels = {"method": method, "outcome": outcome}
            sandbox_duration.record(elapsed, labels)
            sandbox_outcome_total.add(1, labels)
            copilot_session_duration.record(elapsed, {"task_type": task_type})
