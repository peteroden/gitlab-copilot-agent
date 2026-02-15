"""Tests for the GitLab client."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitlab_copilot_agent.gitlab_client import GitLabClient, MRChange, MRDetails, MRDiffRef
from tests.conftest import MR_IID, PROJECT_ID


@pytest.fixture
def mock_gl(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    mr_mock = MagicMock()
    mr_mock.changes.return_value = {
        "title": "Test MR",
        "description": "A test merge request",
        "diff_refs": {
            "base_sha": "aaa111",
            "start_sha": "ccc333",
            "head_sha": "bbb222",
        },
        "changes": [
            {
                "old_path": "src/main.py",
                "new_path": "src/main.py",
                "diff": "@@ -1,3 +1,4 @@\n+new line\n",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
            }
        ],
    }
    mock.projects.get.return_value.mergerequests.get.return_value = mr_mock
    monkeypatch.setattr("gitlab_copilot_agent.gitlab_client.gitlab.Gitlab", lambda *a, **kw: mock)
    return mock


async def test_get_mr_details(mock_gl: MagicMock) -> None:
    client = GitLabClient("https://gitlab.com", "token")
    details = await client.get_mr_details(PROJECT_ID, MR_IID)

    assert isinstance(details, MRDetails)
    assert details.title == "Test MR"
    assert details.description == "A test merge request"
    assert details.diff_refs == MRDiffRef(base_sha="aaa111", start_sha="ccc333", head_sha="bbb222")
    assert len(details.changes) == 1
    assert details.changes[0] == MRChange(
        old_path="src/main.py",
        new_path="src/main.py",
        diff="@@ -1,3 +1,4 @@\n+new line\n",
    )


async def _run(*cmd: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()


async def test_clone_repo_sanitizes_token_in_error(tmp_path: Path) -> None:
    """Test that tokens are sanitized in clone error messages."""
    client = GitLabClient("https://gitlab.com", "token")
    secret = "glpat-secret-token-value"

    # Token sanitization in error from non-existent repo
    with pytest.raises(RuntimeError, match="git clone failed") as exc_info:
        await client.clone_repo("https://gitlab.com/nonexistent/repo.git", "main", secret)
    # Ensure token is not leaked in error message
    assert secret not in str(exc_info.value)
