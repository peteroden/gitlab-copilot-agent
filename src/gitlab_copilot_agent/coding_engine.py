"""Jira-specific prompt builder, coding agent output parsing, and git hygiene."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.prompt_defaults import DEFAULT_CODING_PROMPT, get_prompt
from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams, TaskResult

# Re-export for backward compatibility (canonical source is prompt_defaults)
CODING_SYSTEM_PROMPT = DEFAULT_CODING_PROMPT


class CodingAgentOutput(BaseModel):
    """Structured output expected from the coding agent's final message."""

    model_config = ConfigDict(strict=True)
    summary: str = Field(description="Brief description of changes and test results")
    files_changed: list[str] = Field(
        description="Paths of files intentionally created, modified, or deleted"
    )


_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def parse_agent_output(raw: str) -> CodingAgentOutput | None:
    """Extract and validate the JSON block from the agent's final message.

    Returns None if no valid JSON block is found.
    """
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return None
    try:
        return CodingAgentOutput.model_validate_json(match.group(1).strip())
    except Exception:  # noqa: BLE001 — best-effort parsing
        return None


_PYTHON_GITIGNORE_PATTERNS = [
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    "*.egg-info/",
    "dist/",
    "build/",
    ".venv/",
]


def build_jira_coding_prompt(issue_key: str, summary: str, description: str | None) -> str:
    """Build the user prompt for a Jira coding task."""
    desc_text = description if description else "(no description provided)"
    return (
        f"## Jira Issue: {issue_key}\n"
        f"**Summary:** {summary}\n"
        f"**Description:**\n{desc_text}\n\n"
        f"Implement the changes described in this issue. "
        f"Explore the repository, make necessary changes, run tests, "
        f"and provide a summary of what you did."
    )


def ensure_gitignore(repo_root: str) -> bool:
    """Ensure .gitignore at *repo_root* contains standard Python ignore patterns.

    Returns True if the file was created or modified.
    Refuses to write if .gitignore is a symlink or resolves outside repo_root.
    """
    path = Path(repo_root) / ".gitignore"
    root_resolved = Path(repo_root).resolve()
    if path.is_symlink() or (path.exists() and not path.resolve().is_relative_to(root_resolved)):
        return False
    content = path.read_text() if path.exists() else ""
    existing = set(content.splitlines())
    missing = [p for p in _PYTHON_GITIGNORE_PATTERNS if p not in existing]
    if not missing:
        return False
    suffix = "\n".join(missing) + "\n"
    if content and not content.endswith("\n"):
        suffix = "\n" + suffix
    path.write_text(content + suffix)
    return True


async def run_coding_task(
    executor: TaskExecutor,
    settings: Settings,
    repo_path: str,
    repo_url: str,
    branch: str,
    issue_key: str,
    summary: str,
    description: str | None,
) -> TaskResult:
    """Run a Copilot agent session to implement changes from a Jira issue."""
    ensure_gitignore(repo_path)
    task = TaskParams(
        task_type="coding",
        task_id=issue_key,
        repo_url=repo_url,
        branch=branch,
        system_prompt=get_prompt(settings, "coding"),
        user_prompt=build_jira_coding_prompt(issue_key, summary, description),
        settings=settings,
        repo_path=repo_path,
    )
    return await executor.execute(task)
