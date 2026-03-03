"""Tests for the coding engine."""

from pathlib import Path
from unittest.mock import AsyncMock

from gitlab_copilot_agent.coding_engine import (
    _PYTHON_GITIGNORE_PATTERNS,
    CODING_SYSTEM_PROMPT,
    ensure_git_exclude,
    run_coding_task,
)
from gitlab_copilot_agent.prompt_defaults import get_prompt
from tests.conftest import EXAMPLE_CLONE_URL, make_settings


def test_prompt_includes_gitignore_and_linter_instructions() -> None:
    assert ".gitignore" in CODING_SYSTEM_PROMPT
    assert "linter" in CODING_SYSTEM_PROMPT
    assert "__pycache__" in CODING_SYSTEM_PROMPT


def _init_git_repo(path: Path) -> Path:
    """Create a minimal git repo so .git/info/exclude exists."""
    import subprocess

    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    return path


class TestEnsureGitExclude:
    def test_creates_when_missing(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        assert ensure_git_exclude(str(tmp_path)) is True
        content = (tmp_path / ".git" / "info" / "exclude").read_text()
        for pattern in _PYTHON_GITIGNORE_PATTERNS:
            assert pattern in content

    def test_noop_when_complete(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        exclude = tmp_path / ".git" / "info" / "exclude"
        exclude.write_text("\n".join(_PYTHON_GITIGNORE_PATTERNS) + "\n")
        assert ensure_git_exclude(str(tmp_path)) is False

    def test_appends_missing(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        exclude = tmp_path / ".git" / "info" / "exclude"
        exclude.write_text("__pycache__/\n")
        ensure_git_exclude(str(tmp_path))
        content = exclude.read_text()
        for pattern in _PYTHON_GITIGNORE_PATTERNS:
            assert pattern in content

    def test_returns_false_without_git_dir(self, tmp_path: Path) -> None:
        assert ensure_git_exclude(str(tmp_path)) is False


async def test_run_coding_task_delegates_to_executor(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "Changes completed"

    result = await run_coding_task(
        executor=mock_executor,
        settings=make_settings(),
        repo_path=str(tmp_path),
        repo_url=EXAMPLE_CLONE_URL,
        branch="agent/proj-789",
        issue_key="PROJ-789",
        summary="Implement feature X",
        description="Add X to the codebase",
    )

    assert result == "Changes completed"
    task = mock_executor.execute.call_args[0][0]
    assert task.system_prompt == get_prompt(make_settings(), "coding")
    assert "PROJ-789" in task.user_prompt
    assert (tmp_path / ".git" / "info" / "exclude").exists()
