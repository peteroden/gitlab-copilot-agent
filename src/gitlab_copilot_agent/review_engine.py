"""Copilot review engine — runs an agent review session on an MR."""

import asyncio
from dataclasses import dataclass
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
from gitlab_copilot_agent.repo_config import discover_repo_config

log = structlog.get_logger()

SYSTEM_PROMPT = """\
You are a senior code reviewer. Review the merge request diff thoroughly.

Focus on:
- Bugs, logic errors, and edge cases
- Security vulnerabilities (OWASP Top 10)
- Performance issues
- Code clarity and maintainability

You have access to the full repository via built-in file tools. Use them to
read source files and understand context beyond the diff.

Output your review as a JSON array:
```json
[
  {
    "file": "path/to/file",
    "line": 42,
    "severity": "error|warning|info",
    "comment": "Description of the issue",
    "suggestion": "replacement code for the line(s)",
    "suggestion_start_offset": 0,
    "suggestion_end_offset": 0
  }
]
```

Suggestion fields:
- "suggestion": The replacement code. Include ONLY when you can provide a
  concrete, unambiguous fix. Omit for observations or questions.
- "suggestion_start_offset": Lines ABOVE the commented line to replace (default 0).
- "suggestion_end_offset": Lines BELOW the commented line to replace (default 0).
  For example, to replace just the commented line, use offsets 0, 0.
  To replace a 3-line block (1 above + commented + 1 below), use 1, 1.

After the JSON array, add a brief summary paragraph.
If the code looks good, return an empty array and say so in the summary.
"""


@dataclass(frozen=True)
class ReviewRequest:
    """Minimal info the agent needs to perform a review."""

    title: str
    description: str | None
    source_branch: str
    target_branch: str


def build_review_prompt(req: ReviewRequest) -> str:
    """Build the user prompt — the agent uses git diff and file tools."""
    return (
        f"## Merge Request\n"
        f"**Title:** {req.title}\n"
        f"**Description:** {req.description or '(none)'}\n"
        f"**Source branch:** {req.source_branch}\n"
        f"**Target branch:** {req.target_branch}\n\n"
        f"Review this merge request. Run "
        f"`git diff {req.target_branch}...{req.source_branch}` to see "
        f"the changes, then read relevant files for context."
    )


async def run_review(
    settings: Settings,
    repo_path: str,
    review_request: ReviewRequest,
) -> str:
    """Run a Copilot agent review and return the raw response text."""
    client_opts: CopilotClientOptions = {}
    if settings.github_token:
        client_opts["github_token"] = settings.github_token

    client = CopilotClient(client_opts)
    await client.start()

    try:
        repo_config = discover_repo_config(repo_path)

        system_content = SYSTEM_PROMPT
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
        await session.send({
            "prompt": build_review_prompt(review_request),
        })
        await asyncio.wait_for(done.wait(), timeout=300)
        await session.destroy()

        return messages[-1] if messages else ""
    finally:
        await client.stop()
