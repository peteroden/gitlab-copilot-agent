"""Tests for the GitLab client — httpx-based async implementation."""

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from gitlab_copilot_agent.discussion_models import AgentIdentity
from gitlab_copilot_agent.gitlab_client import (
    GitLabClient,
    MRAuthor,
    MRChange,
    MRCommit,
    MRDetails,
    MRDiffRef,
    NoteListItem,
    _parse_discussions,
    _retry_delay,
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
AUTHOR_ATTRS: dict[str, Any] = {"id": 1, "username": "testuser"}
MR_WEB_URL = f"{GITLAB_URL}/{PROJECT_PATH}/-/merge_requests/{MR_IID}"
DISCUSSION_ID = "abc123discussion"
DIFF_NOTE_PATH = "src/utils.py"
BOT_USER_ID = 99
BOT_USERNAME = "review-bot"
COMPARE_FROM_SHA = "from111"
COMPARE_TO_SHA = "to222"
COMPARE_DIFF = "@@ -1,3 +1,4 @@\n+added\n"
COMPARE_PATH = "src/compare.py"
COMMIT_SHA = "abc123def456"
COMMIT_TITLE = "feat: add new feature"
COMMIT_MESSAGE = "feat: add new feature\n\nDetailed description of the change."
RESOLVE_REPLY_BODY = "✅ Feedback addressed — marking resolved."

# -- Helpers & fixtures --

_DUMMY_REQUEST = httpx.Request("GET", "https://test")


def _resp(data: object, status: int = 200) -> httpx.Response:
    """Build a mock httpx.Response with JSON data."""
    return httpx.Response(status_code=status, json=data, request=_DUMMY_REQUEST)


def _mr_changes_json(**overrides: Any) -> dict[str, Any]:
    """Standard MR changes API response with optional overrides."""
    base: dict[str, Any] = {
        "title": MR_TITLE,
        "description": MR_DESCRIPTION,
        "diff_refs": {"base_sha": BASE_SHA, "start_sha": START_SHA, "head_sha": HEAD_SHA},
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
    return {**base, **overrides}


@pytest.fixture
def client() -> GitLabClient:
    """Pre-configured GitLabClient for tests (not connected to real server)."""
    return GitLabClient(GITLAB_URL, GITLAB_TOKEN)


def _make_raw_note(
    *,
    note_id: int = NOTE_ID,
    body: str = NOTE_BODY,
    system: bool = False,
    note_type: str | None = None,
    resolved: bool | None = None,
    resolvable: bool = False,
    position: dict[str, object] | None = None,
    resolved_by: dict[str, Any] | None | str = "UNSET",
) -> dict[str, object]:
    """Factory for raw note dicts as returned by the GitLab API."""
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
    if resolved_by != "UNSET":
        note["resolved_by"] = resolved_by
    return note


def _make_raw_discussion(discussion_id: str, notes: list[dict[str, object]]) -> dict[str, Any]:
    """Factory for raw discussion dicts from the GitLab API."""
    return {"id": discussion_id, "notes": notes}


# ===================================================================
# get_mr_details
# ===================================================================


async def test_get_mr_details(client: GitLabClient) -> None:
    client._request = AsyncMock(return_value=_resp(_mr_changes_json()))
    details = await client.get_mr_details(PROJECT_ID, MR_IID)

    assert details == MRDetails(
        title=MR_TITLE,
        description=MR_DESCRIPTION,
        diff_refs=MRDiffRef(base_sha=BASE_SHA, start_sha=START_SHA, head_sha=HEAD_SHA),
        changes=[MRChange(old_path=OLD_PATH, new_path=OLD_PATH, diff=DIFF_CONTENT)],
    )


async def test_get_mr_details_retries_null_diff_refs(client: GitLabClient) -> None:
    """Retries when diff_refs is null (GitLab race on new MRs)."""
    client._request = AsyncMock(
        side_effect=[_resp(_mr_changes_json(diff_refs=None)), _resp(_mr_changes_json())]
    )
    details = await client.get_mr_details(PROJECT_ID, MR_IID)
    assert details.diff_refs.head_sha == HEAD_SHA
    assert client._request.await_count == 2


async def test_get_mr_details_raises_after_null_diff_refs_exhausted(client: GitLabClient) -> None:
    client._request = AsyncMock(return_value=_resp(_mr_changes_json(diff_refs=None)))
    with pytest.raises(RuntimeError, match="diff_refs is null"):
        await client.get_mr_details(PROJECT_ID, MR_IID)


# ===================================================================
# Paginated list endpoints
# ===================================================================


async def test_list_project_mrs(client: GitLabClient) -> None:
    mr_data = {
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
    client._paginate = AsyncMock(return_value=[mr_data])

    result = await client.list_project_mrs(PROJECT_ID, state="merged", updated_after=ISO_TIMESTAMP)

    assert len(result) == 1
    assert result[0].iid == MR_IID
    assert result[0].author == MRAuthor(id=1, username="testuser")
    params = client._paginate.call_args[1]["params"]
    assert params["state"] == "merged"
    assert params["updated_after"] == ISO_TIMESTAMP


async def test_list_project_mrs_defaults(client: GitLabClient) -> None:
    client._paginate = AsyncMock(return_value=[])
    assert await client.list_project_mrs(PROJECT_ID) == []
    assert client._paginate.call_args[1]["params"]["state"] == "opened"


async def test_list_mr_notes(client: GitLabClient) -> None:
    note_data = {
        "id": NOTE_ID,
        "body": NOTE_BODY,
        "author": AUTHOR_ATTRS,
        "system": False,
        "created_at": ISO_TIMESTAMP,
    }
    client._paginate = AsyncMock(return_value=[note_data])

    result = await client.list_mr_notes(PROJECT_ID, MR_IID, created_after=ISO_TIMESTAMP)

    assert len(result) == 1
    assert result[0] == NoteListItem(
        id=NOTE_ID, body=NOTE_BODY, author=MRAuthor(**AUTHOR_ATTRS), created_at=ISO_TIMESTAMP
    )


async def test_list_mr_notes_defaults(client: GitLabClient) -> None:
    client._paginate = AsyncMock(return_value=[])
    assert await client.list_mr_notes(PROJECT_ID, MR_IID) == []


# ===================================================================
# resolve_project
# ===================================================================


async def test_resolve_project_by_id(client: GitLabClient) -> None:
    assert await client.resolve_project(PROJECT_ID) == PROJECT_ID


async def test_resolve_project_by_path(client: GitLabClient) -> None:
    client._request = AsyncMock(return_value=_resp({"id": PROJECT_ID}))
    assert await client.resolve_project(PROJECT_PATH) == PROJECT_ID
    assert "group%2Fmy-project" in client._request.call_args[0][1]


# ===================================================================
# clone_repo
# ===================================================================


async def test_clone_repo_sanitizes_token_in_error(client: GitLabClient) -> None:
    with pytest.raises(RuntimeError, match="git clone failed") as exc_info:
        await client.clone_repo("https://gitlab.com/nonexistent/repo.git", "main", SECRET_TOKEN)
    assert SECRET_TOKEN not in str(exc_info.value)


# ===================================================================
# _parse_discussions
# ===================================================================


async def test_discussions_mixed_types() -> None:
    """DiffNote -> is_inline=True; DiscussionNote -> is_inline=False."""
    raw = [
        _make_raw_discussion(
            "d1",
            [
                _make_raw_note(
                    note_id=10,
                    note_type="DiffNote",
                    position={
                        "new_path": DIFF_NOTE_PATH,
                        "old_path": DIFF_NOTE_PATH,
                        "new_line": 5,
                        "old_line": None,
                    },
                )
            ],
        ),
        _make_raw_discussion("d2", [_make_raw_note(note_id=20, note_type="DiscussionNote")]),
    ]
    result = _parse_discussions(raw)
    assert [(d.is_inline, d.notes[0].note_id) for d in result] == [(True, 10), (False, 20)]


async def test_discussions_system_notes_filtered() -> None:
    raw = [
        _make_raw_discussion(
            "d3",
            [_make_raw_note(note_id=30, system=True), _make_raw_note(note_id=31)],
        )
    ]
    result = _parse_discussions(raw)
    assert len(result) == 1
    assert [n.note_id for n in result[0].notes] == [31]


async def test_discussions_all_system_notes_dropped() -> None:
    raw = [_make_raw_discussion("d4", [_make_raw_note(note_id=40, system=True)])]
    assert _parse_discussions(raw) == []


async def test_discussions_position_extraction() -> None:
    pos = {"new_path": DIFF_NOTE_PATH, "old_path": "src/old.py", "new_line": 42, "old_line": 10}
    raw = [
        _make_raw_discussion(
            "d5", [_make_raw_note(note_id=50, note_type="DiffNote", position=pos)]
        )
    ]
    note = _parse_discussions(raw)[0].notes[0]
    assert note.position is not None
    assert (note.position["new_path"], note.position["new_line"]) == (DIFF_NOTE_PATH, 42)


async def test_discussions_empty(client: GitLabClient) -> None:
    client._paginate = AsyncMock(return_value=[])
    assert await client.list_mr_discussions(PROJECT_ID, MR_IID) == []


@pytest.mark.parametrize(
    ("resolved_by", "expected_id"),
    [
        ({"id": 42, "username": "human-dev"}, 42),
        ("UNSET", None),  # key absent
        (None, None),  # key present but null
    ],
    ids=["present", "absent", "null"],
)
async def test_discussions_resolved_by(
    resolved_by: dict[str, Any] | None | str,
    expected_id: int | None,
) -> None:
    note = _make_raw_note(note_id=60, resolved=True, resolvable=True, resolved_by=resolved_by)
    raw = [_make_raw_discussion("d-rb", [note])]
    assert _parse_discussions(raw)[0].notes[0].resolved_by_id == expected_id


# ===================================================================
# Simple write endpoints (parametrized)
# ===================================================================


@pytest.mark.parametrize(
    ("method_name", "args", "expected_verb", "expected_json"),
    [
        ("resolve_discussion", (PROJECT_ID, MR_IID, DISCUSSION_ID), "PUT", {"resolved": True}),
        (
            "reply_to_discussion",
            (PROJECT_ID, MR_IID, DISCUSSION_ID, RESOLVE_REPLY_BODY),
            "POST",
            {"body": RESOLVE_REPLY_BODY},
        ),
        ("post_mr_comment", (PROJECT_ID, MR_IID, "Great work!"), "POST", {"body": "Great work!"}),
    ],
    ids=["resolve_discussion", "reply_to_discussion", "post_mr_comment"],
)
async def test_write_endpoint(
    client: GitLabClient,
    method_name: str,
    args: tuple[Any, ...],
    expected_verb: str,
    expected_json: dict[str, Any],
) -> None:
    client._request = AsyncMock(return_value=_resp({}))
    await getattr(client, method_name)(*args)
    call = client._request.call_args
    assert call[0][0] == expected_verb
    assert call[1]["json"] == expected_json


async def test_create_mr_discussion(client: GitLabClient) -> None:
    client._request = AsyncMock(return_value=_resp({}))
    position = {"base_sha": BASE_SHA, "position_type": "text", "new_line": 10}
    await client.create_mr_discussion(PROJECT_ID, MR_IID, "Bug", position)
    call = client._request.call_args
    assert call[0][0] == "POST"
    assert call[1]["json"] == {"body": "Bug", "position": position}


async def test_create_merge_request(client: GitLabClient) -> None:
    client._request = AsyncMock(return_value=_resp({"iid": 42}))
    assert await client.create_merge_request(PROJECT_ID, "feat", "main", "T", "D") == 42


# ===================================================================
# get_current_user
# ===================================================================


async def test_get_current_user(client: GitLabClient) -> None:
    client._request = AsyncMock(return_value=_resp({"id": BOT_USER_ID, "username": BOT_USERNAME}))
    identity = await client.get_current_user()
    assert identity == AgentIdentity(user_id=BOT_USER_ID, username=BOT_USERNAME)


# ===================================================================
# compare_commits
# ===================================================================


async def test_compare_commits(client: GitLabClient) -> None:
    client._request = AsyncMock(
        return_value=_resp(
            {
                "diffs": [
                    {
                        "old_path": COMPARE_PATH,
                        "new_path": COMPARE_PATH,
                        "diff": COMPARE_DIFF,
                        "new_file": False,
                        "deleted_file": False,
                        "renamed_file": False,
                    }
                ]
            }
        )
    )
    result = await client.compare_commits(PROJECT_ID, COMPARE_FROM_SHA, COMPARE_TO_SHA)
    assert len(result) == 1 and result[0].diff == COMPARE_DIFF


async def test_compare_commits_empty(client: GitLabClient) -> None:
    client._request = AsyncMock(return_value=_resp({"diffs": []}))
    assert await client.compare_commits(PROJECT_ID, COMPARE_FROM_SHA, COMPARE_TO_SHA) == []


# ===================================================================
# get_mr_commits
# ===================================================================


async def test_get_mr_commits(client: GitLabClient) -> None:
    client._paginate = AsyncMock(
        return_value=[{"id": COMMIT_SHA, "title": COMMIT_TITLE, "message": COMMIT_MESSAGE}]
    )
    result = await client.get_mr_commits(PROJECT_ID, MR_IID)
    assert len(result) == 1 and result[0].id == COMMIT_SHA


async def test_get_mr_commits_empty(client: GitLabClient) -> None:
    client._paginate = AsyncMock(return_value=[])
    assert await client.get_mr_commits(PROJECT_ID, MR_IID) == []


# ===================================================================
# Error propagation (parametrized)
# ===================================================================


@pytest.mark.parametrize(
    ("method_name", "mock_attr", "args"),
    [
        ("compare_commits", "_request", (PROJECT_ID, COMPARE_FROM_SHA, COMPARE_TO_SHA)),
        ("get_mr_commits", "_paginate", (PROJECT_ID, MR_IID)),
    ],
    ids=["compare_commits", "get_mr_commits"],
)
async def test_error_propagation(
    client: GitLabClient,
    method_name: str,
    mock_attr: str,
    args: tuple[Any, ...],
) -> None:
    setattr(client, mock_attr, AsyncMock(side_effect=RuntimeError("API failure")))
    with pytest.raises(RuntimeError, match="API failure"):
        await getattr(client, method_name)(*args)


# ===================================================================
# MRCommit model
# ===================================================================


def test_mr_commit_validation() -> None:
    commit = MRCommit.model_validate(
        {"id": COMMIT_SHA, "title": COMMIT_TITLE, "message": COMMIT_MESSAGE}
    )
    assert (commit.id, commit.title) == (COMMIT_SHA, COMMIT_TITLE)


def test_mr_commit_extra_fields_ignored() -> None:
    commit = MRCommit.model_validate(
        {"id": COMMIT_SHA, "title": COMMIT_TITLE, "message": COMMIT_MESSAGE, "author_name": "X"}
    )
    assert not hasattr(commit, "author_name")


def test_mr_commit_frozen() -> None:
    commit = MRCommit(id=COMMIT_SHA, title=COMMIT_TITLE, message=COMMIT_MESSAGE)
    with pytest.raises(Exception):  # noqa: B017
        commit.id = "new"  # type: ignore[misc]


# ===================================================================
# _request retry behavior
# ===================================================================


async def test_request_retries_get_on_429(client: GitLabClient) -> None:
    rate_limited = httpx.Response(
        status_code=429, headers={"retry-after": "0.01"}, request=_DUMMY_REQUEST
    )
    ok = httpx.Response(status_code=200, json={}, request=_DUMMY_REQUEST)
    client._client = AsyncMock()
    client._client.request = AsyncMock(side_effect=[rate_limited, ok])

    assert (await client._request("GET", "/t")).status_code == 200
    assert client._client.request.await_count == 2


async def test_request_no_retry_post_on_500(client: GitLabClient) -> None:
    client._client = AsyncMock()
    client._client.request = AsyncMock(
        return_value=httpx.Response(status_code=500, request=_DUMMY_REQUEST)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client._request("POST", "/t")
    assert client._client.request.await_count == 1


async def test_request_retries_get_on_transport_error(client: GitLabClient) -> None:
    ok = httpx.Response(status_code=200, json={}, request=_DUMMY_REQUEST)
    client._client = AsyncMock()
    client._client.request = AsyncMock(side_effect=[httpx.ConnectError("refused"), ok])
    assert (await client._request("GET", "/t")).status_code == 200


# ===================================================================
# _retry_delay (parametrized)
# ===================================================================


@pytest.mark.parametrize(
    ("status", "headers", "attempt", "expected"),
    [
        (429, {"retry-after": "5"}, 0, 5.0),
        (500, {}, 0, 1.0),
        (500, {}, 1, 2.0),
        (500, {}, 2, 4.0),
    ],
    ids=["retry-after-header", "backoff-0", "backoff-1", "backoff-2"],
)
def test_retry_delay(status: int, headers: dict[str, str], attempt: int, expected: float) -> None:
    resp = httpx.Response(status_code=status, headers=headers, request=_DUMMY_REQUEST)
    assert _retry_delay(resp, attempt) == expected


# ===================================================================
# Lifecycle (aclose / context manager)
# ===================================================================


async def test_aclose(client: GitLabClient) -> None:
    await client.aclose()
    with pytest.raises(RuntimeError):
        await client._client.get("/test")


async def test_context_manager() -> None:
    async with GitLabClient(GITLAB_URL, GITLAB_TOKEN) as c:
        assert c is not None


# ===================================================================
# _paginate
# ===================================================================


async def test_paginate_single_page(client: GitLabClient) -> None:
    client._request = AsyncMock(return_value=_resp([{"id": i} for i in range(5)]))
    assert len(await client._paginate("/t")) == 5
    assert client._request.await_count == 1


async def test_paginate_multiple_pages(client: GitLabClient) -> None:
    page1 = [{"id": i} for i in range(100)]
    page2 = [{"id": i} for i in range(100, 110)]
    client._request = AsyncMock(side_effect=[_resp(page1), _resp(page2)])
    result = await client._paginate("/t")
    assert len(result) == 110 and client._request.await_count == 2
