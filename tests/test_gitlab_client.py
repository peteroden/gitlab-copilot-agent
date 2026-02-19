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


async def test_list_project_mrs(mock_gl: MagicMock) -> None:
    mr_obj = MagicMock()
    mr_obj.attributes = {"iid": MR_IID, "title": "Test MR", "state": "opened"}
    mock_gl.projects.get.return_value.mergerequests.list.return_value = [mr_obj]

    client = GitLabClient("https://gitlab.com", "token")
    result = await client.list_project_mrs(
        PROJECT_ID, state="merged", updated_after="2024-01-01T00:00:00Z"
    )

    assert result == [{"iid": MR_IID, "title": "Test MR", "state": "opened"}]
    mock_gl.projects.get.return_value.mergerequests.list.assert_called_once_with(
        state="merged", get_all=True, updated_after="2024-01-01T00:00:00Z"
    )


async def test_list_project_mrs_defaults(mock_gl: MagicMock) -> None:
    mock_gl.projects.get.return_value.mergerequests.list.return_value = []

    client = GitLabClient("https://gitlab.com", "token")
    result = await client.list_project_mrs(PROJECT_ID)

    assert result == []
    mock_gl.projects.get.return_value.mergerequests.list.assert_called_once_with(
        state="opened", get_all=True
    )


async def test_list_mr_notes(mock_gl: MagicMock) -> None:
    note_obj = MagicMock()
    note_obj.attributes = {"id": 1, "body": "LGTM"}
    mock_gl.projects.get.return_value.mergerequests.get.return_value.notes.list.return_value = [
        note_obj
    ]

    client = GitLabClient("https://gitlab.com", "token")
    result = await client.list_mr_notes(PROJECT_ID, MR_IID, created_after="2024-01-01T00:00:00Z")

    assert result == [{"id": 1, "body": "LGTM"}]
    mock_gl.projects.get.return_value.mergerequests.get.return_value.notes.list.assert_called_once_with(
        get_all=True, created_after="2024-01-01T00:00:00Z"
    )


async def test_list_mr_notes_defaults(mock_gl: MagicMock) -> None:
    mock_gl.projects.get.return_value.mergerequests.get.return_value.notes.list.return_value = []

    client = GitLabClient("https://gitlab.com", "token")
    result = await client.list_mr_notes(PROJECT_ID, MR_IID)

    assert result == []
    mock_gl.projects.get.return_value.mergerequests.get.return_value.notes.list.assert_called_once_with(
        get_all=True
    )


async def test_resolve_project_by_id(mock_gl: MagicMock) -> None:
    mock_gl.projects.get.return_value.id = PROJECT_ID

    client = GitLabClient("https://gitlab.com", "token")
    result = await client.resolve_project(PROJECT_ID)

    assert result == PROJECT_ID
    mock_gl.projects.get.assert_called_with(PROJECT_ID)


async def test_resolve_project_by_path(mock_gl: MagicMock) -> None:
    mock_gl.projects.get.return_value.id = PROJECT_ID

    client = GitLabClient("https://gitlab.com", "token")
    result = await client.resolve_project("group/my-project")

    assert result == PROJECT_ID
    mock_gl.projects.get.assert_called_with("group/my-project")


async def test_clone_repo_sanitizes_token_in_error(tmp_path: Path) -> None:
    """Test that tokens are sanitized in clone error messages."""
    client = GitLabClient("https://gitlab.com", "token")
    secret = "glpat-secret-token-value"

    # Token sanitization in error from non-existent repo
    with pytest.raises(RuntimeError, match="git clone failed") as exc_info:
        await client.clone_repo("https://gitlab.com/nonexistent/repo.git", "main", secret)
    # Ensure token is not leaked in error message
    assert secret not in str(exc_info.value)
