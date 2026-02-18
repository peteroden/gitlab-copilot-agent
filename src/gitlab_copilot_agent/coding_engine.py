"""Shared coding system prompt, Jira-specific prompt builder, and git hygiene."""

from __future__ import annotations

from pathlib import Path

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.task_executor import TaskExecutor, TaskParams

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
4. Follow existing project conventions (code style, patterns, architecture)
5. Ensure .gitignore exists with standard ignores for the project language
6. Run the project linter if available and fix any issues
7. Run tests if available to verify your changes
8. Output a summary of changes made

Guidelines:
- Make the smallest change that solves the problem
- Preserve existing behavior unless explicitly required to change it
- Follow SOLID principles and existing patterns
- Add tests for new functionality — test behavior, not error message strings
- Update documentation if needed
- Do not introduce new dependencies without strong justification
- Never commit generated or cached files (__pycache__, .pyc, node_modules, etc.)

Output format:
Provide a summary of:
- Files modified or created
- Key changes made
- Test results (if tests were run)
- Any concerns or follow-up items
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
) -> str:
    """Run a Copilot agent session to implement changes from a Jira issue."""
    ensure_gitignore(repo_path)
    task = TaskParams(
        task_type="coding",
        task_id=issue_key,
        repo_url=repo_url,
        branch=branch,
        system_prompt=CODING_SYSTEM_PROMPT,
        user_prompt=build_jira_coding_prompt(issue_key, summary, description),
        settings=settings,
        repo_path=repo_path,
    )
    return await executor.execute(task)
