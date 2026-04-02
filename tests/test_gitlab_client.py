"""Tests for the GitLab client."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitlab_copilot_agent.discussion_models import AgentIdentity
from gitlab_copilot_agent.gitlab_client import (
    GitLabClient,
    MRAuthor,
    MRChange,
    MRDetails,
    MRDiffRef,
    MRListItem,
    NoteListItem,
)
from tests.conftest import GITLAB_TOKEN, GITLAB_URL, MR_IID, PROJECT_ID

MR_TITLE = "Test MR"
MR_DESCRIPTION = "A test merge request"
BASE_SHA = "aaa111"
START_SHA = "ccc333"
HEAD_SHA = "bbb222"
OLD_PATH = "src/main.py"
DIFF_CONTENT = "@@ -1,3 +1,4 @@\n+new line\n"
NOTE_BODY = "LGTM"
NOTE_ID = 1
PROJECT_PATH = "group/my-project"
ISO_TIMESTAMP = "2024-01-01T00:00:00Z"
SECRET_TOKEN = "glpat-secret-token-value"
AUTHOR_ATTRS = {"id": 1, "username": "testuser"}
MR_WEB_URL = f"{GITLAB_URL}/{PROJECT_PATH}/-/merge_requests/{MR_IID}"
DISCUSSION_ID = "abc123discussion"
DIFF_NOTE_PATH = "src/utils.py"
BOT_USER_ID = 99
BOT_USERNAME = "review-bot"


@pytest.fixture
def mock_gl(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    mr_mock = MagicMock()
    mr_mock.changes.return_value = {
        "title": MR_TITLE,
        "description": MR_DESCRIPTION,
        "diff_refs": {
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD_SHA,
        },
        "changes": [
            {
                "old_path": OLD_PATH,
                "new_path": OLD_PATH,
                "diff": DIFF_CONTENT,
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
    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    details = await client.get_mr_details(PROJECT_ID, MR_IID)

    assert isinstance(details, MRDetails)
    assert details.title == MR_TITLE
    assert details.description == MR_DESCRIPTION
    expected_refs = MRDiffRef(base_sha=BASE_SHA, start_sha=START_SHA, head_sha=HEAD_SHA)
    assert details.diff_refs == expected_refs
    assert len(details.changes) == 1
    assert details.changes[0] == MRChange(
        old_path=OLD_PATH,
        new_path=OLD_PATH,
        diff=DIFF_CONTENT,
    )


async def _run(*cmd: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()


async def test_list_project_mrs(mock_gl: MagicMock) -> None:
    mr_obj = MagicMock()
    mr_obj.attributes = {
        "iid": MR_IID,
        "title": MR_TITLE,
        "description": MR_DESCRIPTION,
        "source_branch": "feature",
        "target_branch": "main",
        "sha": HEAD_SHA,
        "web_url": MR_WEB_URL,
        "state": "opened",
        "author": AUTHOR_ATTRS,
        "updated_at": ISO_TIMESTAMP,
    }
    mock_gl.projects.get.return_value.mergerequests.list.return_value = [mr_obj]

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.list_project_mrs(PROJECT_ID, state="merged", updated_after=ISO_TIMESTAMP)

    assert len(result) == 1
    assert isinstance(result[0], MRListItem)
    assert result[0].iid == MR_IID
    assert result[0].sha == HEAD_SHA
    assert result[0].author == MRAuthor(id=1, username="testuser")
    mock_gl.projects.get.return_value.mergerequests.list.assert_called_once_with(
        state="merged", get_all=True, updated_after=ISO_TIMESTAMP
    )


async def test_list_project_mrs_defaults(mock_gl: MagicMock) -> None:
    mock_gl.projects.get.return_value.mergerequests.list.return_value = []

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.list_project_mrs(PROJECT_ID)

    assert result == []
    mock_gl.projects.get.return_value.mergerequests.list.assert_called_once_with(
        state="opened", get_all=True
    )


async def test_list_mr_notes(mock_gl: MagicMock) -> None:
    note_obj = MagicMock()
    note_obj.attributes = {
        "id": NOTE_ID,
        "body": NOTE_BODY,
        "author": AUTHOR_ATTRS,
        "system": False,
        "created_at": ISO_TIMESTAMP,
    }
    mock_gl.projects.get.return_value.mergerequests.get.return_value.notes.list.return_value = [
        note_obj
    ]

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.list_mr_notes(PROJECT_ID, MR_IID, created_after=ISO_TIMESTAMP)

    assert len(result) == 1
    assert isinstance(result[0], NoteListItem)
    assert result[0].id == NOTE_ID
    assert result[0].body == NOTE_BODY
    mock_gl.projects.get.return_value.mergerequests.get.return_value.notes.list.assert_called_once_with(
        get_all=True, created_after=ISO_TIMESTAMP
    )


async def test_list_mr_notes_defaults(mock_gl: MagicMock) -> None:
    mock_gl.projects.get.return_value.mergerequests.get.return_value.notes.list.return_value = []

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.list_mr_notes(PROJECT_ID, MR_IID)

    assert result == []
    mock_gl.projects.get.return_value.mergerequests.get.return_value.notes.list.assert_called_once_with(
        get_all=True
    )


async def test_resolve_project_by_id(mock_gl: MagicMock) -> None:
    mock_gl.projects.get.return_value.id = PROJECT_ID

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.resolve_project(PROJECT_ID)

    assert result == PROJECT_ID
    mock_gl.projects.get.assert_called_with(PROJECT_ID)


async def test_resolve_project_by_path(mock_gl: MagicMock) -> None:
    mock_gl.projects.get.return_value.id = PROJECT_ID

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.resolve_project(PROJECT_PATH)

    assert result == PROJECT_ID
    mock_gl.projects.get.assert_called_with(PROJECT_PATH)


async def test_clone_repo_sanitizes_token_in_error(tmp_path: Path) -> None:
    """Test that tokens are sanitized in clone error messages."""
    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)

    with pytest.raises(RuntimeError, match="git clone failed") as exc_info:
        await client.clone_repo("https://gitlab.com/nonexistent/repo.git", "main", SECRET_TOKEN)
    assert SECRET_TOKEN not in str(exc_info.value)


# -- list_mr_discussions tests --


def _make_raw_note(
    *,
    note_id: int = NOTE_ID,
    body: str = NOTE_BODY,
    system: bool = False,
    note_type: str | None = None,
    resolved: bool | None = None,
    resolvable: bool = False,
    position: dict[str, object] | None = None,
) -> dict[str, object]:
    """Factory for raw discussion note dicts as returned by python-gitlab."""
    note: dict[str, object] = {
        "id": note_id,
        "body": body,
        "author": AUTHOR_ATTRS,
        "system": system,
        "created_at": ISO_TIMESTAMP,
        "resolvable": resolvable,
    }
    if note_type is not None:
        note["type"] = note_type
    if resolved is not None:
        note["resolved"] = resolved
    if position is not None:
        note["position"] = position
    return note


def _make_discussion_mock(discussion_id: str, notes: list[dict[str, object]]) -> MagicMock:
    """Factory for a raw discussion object with .attributes."""
    disc = MagicMock()
    disc.attributes = {"id": discussion_id, "notes": notes}
    return disc


async def test_list_mr_discussions_mixed_types(mock_gl: MagicMock) -> None:
    """DiffNote sets is_inline=True; DiscussionNote keeps is_inline=False."""
    diff_note = _make_raw_note(
        note_id=10,
        body="inline comment",
        note_type="DiffNote",
        position={
            "new_path": DIFF_NOTE_PATH,
            "old_path": DIFF_NOTE_PATH,
            "new_line": 5,
            "old_line": None,
        },
    )
    regular_note = _make_raw_note(note_id=20, body="general comment", note_type="DiscussionNote")

    disc_inline = _make_discussion_mock("d1", [diff_note])
    disc_general = _make_discussion_mock("d2", [regular_note])

    mr_mock = mock_gl.projects.get.return_value.mergerequests.get.return_value
    mr_mock.discussions.list.return_value = [disc_inline, disc_general]

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.list_mr_discussions(PROJECT_ID, MR_IID)

    assert len(result) == 2
    assert result[0].is_inline is True
    assert result[0].notes[0].note_id == 10
    assert result[1].is_inline is False
    assert result[1].notes[0].note_id == 20


async def test_list_mr_discussions_system_notes_filtered(mock_gl: MagicMock) -> None:
    """System notes are excluded from the returned discussion."""
    system_note = _make_raw_note(note_id=30, body="system event", system=True)
    user_note = _make_raw_note(note_id=31, body="user reply")

    disc = _make_discussion_mock("d3", [system_note, user_note])
    mr_mock = mock_gl.projects.get.return_value.mergerequests.get.return_value
    mr_mock.discussions.list.return_value = [disc]

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.list_mr_discussions(PROJECT_ID, MR_IID)

    assert len(result) == 1
    assert len(result[0].notes) == 1
    assert result[0].notes[0].note_id == 31


async def test_list_mr_discussions_all_system_notes_skipped(mock_gl: MagicMock) -> None:
    """Discussions where ALL notes are system notes are dropped entirely."""
    system_only = _make_raw_note(note_id=40, body="system", system=True)
    disc = _make_discussion_mock("d4", [system_only])

    mr_mock = mock_gl.projects.get.return_value.mergerequests.get.return_value
    mr_mock.discussions.list.return_value = [disc]

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.list_mr_discussions(PROJECT_ID, MR_IID)

    assert result == []


async def test_list_mr_discussions_position_extraction(mock_gl: MagicMock) -> None:
    """DiffNote position fields are correctly extracted."""
    position = {
        "new_path": DIFF_NOTE_PATH,
        "old_path": "src/old_utils.py",
        "new_line": 42,
        "old_line": 10,
    }
    diff_note = _make_raw_note(note_id=50, note_type="DiffNote", position=position)
    disc = _make_discussion_mock("d5", [diff_note])

    mr_mock = mock_gl.projects.get.return_value.mergerequests.get.return_value
    mr_mock.discussions.list.return_value = [disc]

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.list_mr_discussions(PROJECT_ID, MR_IID)

    note = result[0].notes[0]
    assert note.position is not None
    assert note.position["new_path"] == DIFF_NOTE_PATH
    assert note.position["old_path"] == "src/old_utils.py"
    assert note.position["new_line"] == 42
    assert note.position["old_line"] == 10


async def test_list_mr_discussions_empty(mock_gl: MagicMock) -> None:
    """Empty discussions list returns empty result."""
    mr_mock = mock_gl.projects.get.return_value.mergerequests.get.return_value
    mr_mock.discussions.list.return_value = []

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    result = await client.list_mr_discussions(PROJECT_ID, MR_IID)

    assert result == []
    mr_mock.discussions.list.assert_called_once_with(get_all=True)


# -- get_current_user tests --


async def test_get_current_user(mock_gl: MagicMock) -> None:
    """Returns AgentIdentity with user_id and username from authenticated user."""
    mock_gl.user = MagicMock()
    mock_gl.user.id = BOT_USER_ID
    mock_gl.user.username = BOT_USERNAME

    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
    identity = await client.get_current_user()

    mock_gl.auth.assert_called_once()
    assert isinstance(identity, AgentIdentity)
    assert identity.user_id == BOT_USER_ID
    assert identity.username == BOT_USERNAME
