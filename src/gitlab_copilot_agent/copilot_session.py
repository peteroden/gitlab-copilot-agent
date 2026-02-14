"""Shared Copilot SDK session runner — extracted from review_engine."""

import asyncio
import os
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
from gitlab_copilot_agent.process_sandbox import get_sandbox
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


async def run_copilot_session(
    settings: Settings,
    repo_path: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 300,
) -> str:
    """Run a Copilot agent session and return the last assistant message."""
    with _tracer.start_as_current_span(
        "copilot.session",
        attributes={"repo_path": repo_path, "timeout": timeout},
    ):
        sandbox = get_sandbox()
        cli_wrapper = sandbox.create_cli_wrapper(repo_path)
        try:
            client_opts: CopilotClientOptions = {
                "cli_path": cli_wrapper,
                "env": build_sdk_env(settings.github_token),
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

                return messages[-1] if messages else ""
            finally:
                await client.stop()
        finally:
            sandbox.cleanup()
