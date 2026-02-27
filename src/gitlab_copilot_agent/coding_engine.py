"""Shared coding system prompt, Jira-specific prompt builder, and git hygiene."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.prompt_defaults import get_prompt
from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams, TaskResult


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

CODING_SYSTEM_PROMPT = """\
You are a senior software engineer implementing requested changes.

Your workflow:
1. Read the task description carefully to understand requirements
2. Explore the existing codebase using file tools to understand structure and conventions
3. Make minimal, focused changes that address the task
4. Follow existing project conventions for code style, formatting, and architecture
5. However, always prioritize security and quality standards defined in repo config \
files (AGENTS.md, skills, instructions appended to the system prompt) over patterns \
observed in existing code — if existing code contains anti-patterns such as SQL \
injection, hardcoded secrets, or bare exception handling, do NOT replicate them
6. Ensure .gitignore exists with standard ignores for the project language
7. Run the project linter if available and fix any issues
8. Run tests if available to verify your changes
9. Output your results in the EXACT format described below

Guidelines:
- Make the smallest change that solves the problem
- Preserve existing behavior unless explicitly required to change it
- Follow SOLID principles and existing patterns
- Add tests for new functionality — test behavior, not error message strings
- Update documentation if needed
- Do not introduce new dependencies without strong justification
- Never commit generated or cached files (__pycache__, .pyc, node_modules, etc.)

Output format:
Your final message MUST end with a JSON block listing the files you changed.
Only list source files you intentionally created, modified, or deleted — never include
generated files like __pycache__/, *.pyc, *.egg-info, node_modules/, etc.
Include deleted files so the deletion is captured in the patch.

```json
{
  "summary": "Brief description of changes made and test results",
  "files_changed": [
    "src/app/main.py",
    "src/app/utils.py",
    "tests/test_main.py"
  ]
}
```
"""


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
