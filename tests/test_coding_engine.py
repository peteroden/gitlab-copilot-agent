"""Tests for the coding engine."""

from pathlib import Path
from unittest.mock import AsyncMock

from gitlab_copilot_agent.coding_engine import (
    _PYTHON_GITIGNORE_PATTERNS,
    CODING_SYSTEM_PROMPT,
    ensure_gitignore,
    run_coding_task,
)
from tests.conftest import EXAMPLE_CLONE_URL, make_settings


def test_prompt_includes_gitignore_and_linter_instructions() -> None:
    assert ".gitignore" in CODING_SYSTEM_PROMPT
    assert "linter" in CODING_SYSTEM_PROMPT
    assert "__pycache__" in CODING_SYSTEM_PROMPT


class TestEnsureGitignore:
    def test_creates_when_missing(self, tmp_path: Path) -> None:
        assert ensure_gitignore(str(tmp_path)) is True
        content = (tmp_path / ".gitignore").read_text()
        for pattern in _PYTHON_GITIGNORE_PATTERNS:
            assert pattern in content

    def test_noop_when_complete(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("\n".join(_PYTHON_GITIGNORE_PATTERNS) + "\n")
        assert ensure_gitignore(str(tmp_path)) is False

    def test_appends_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("__pycache__/\n")
        ensure_gitignore(str(tmp_path))
        content = (tmp_path / ".gitignore").read_text()
        for pattern in _PYTHON_GITIGNORE_PATTERNS:
            assert pattern in content

    def test_refuses_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere.txt"
        target.write_text("original")
        (tmp_path / ".gitignore").symlink_to(target)
        assert ensure_gitignore(str(tmp_path)) is False
        assert target.read_text() == "original"


async def test_run_coding_task_delegates_to_executor(tmp_path: Path) -> None:
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
    assert task.system_prompt == CODING_SYSTEM_PROMPT
    assert "PROJ-789" in task.user_prompt
    assert (tmp_path / ".gitignore").exists()
