"""Tests for the discussion handler — handle_discussion_interaction pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gitlab_copilot_agent.discussion_engine import DiscussionResponse
from gitlab_copilot_agent.discussion_models import (
    AgentIdentity,
    Discussion,
    DiscussionNote,
)
from gitlab_copilot_agent.models import (
    NoteMergeRequest,
    NoteObjectAttributes,
    NoteWebhookPayload,
    WebhookProject,
    WebhookUser,
)
from gitlab_copilot_agent.task_executor import CodingResult, ReviewResult, TaskExecutionError
from tests.conftest import make_settings

# -- Test constants --

PROJECT_ID = 42
MR_IID = 7
NOTE_ID = 501
DISCUSSION_ID = "disc-001"
AGENT_USER_ID = 99
AGENT_USERNAME = "review-bot"
SOURCE_BRANCH = "feature/test"
TARGET_BRANCH = "main"
CLONE_URL = "https://gitlab.com/group/project.git"
PROJECT_PATH = "group/project"
NOTE_TEXT = "@review-bot please explain this"
MR_TITLE = "Test MR"
REPLY_TEXT = "Here is my explanation."
CHANGES_PUSHED_MARKER = "✅ Changes pushed."
GENERIC_ERROR_SNIPPET = "❌ Unable to process your request"

# -- Module path prefix for patches --
_MOD = "gitlab_copilot_agent.discussion_orchestrator"


# -- Factories --


def _make_payload(**overrides: Any) -> NoteWebhookPayload:
    defaults: dict[str, Any] = {
        "object_kind": "note",
        "user": WebhookUser(id=1, username="developer"),
        "project": WebhookProject(
            id=PROJECT_ID,
            path_with_namespace=PROJECT_PATH,
            git_http_url=CLONE_URL,
        ),
        "object_attributes": NoteObjectAttributes(
            id=NOTE_ID,
            note=NOTE_TEXT,
            noteable_type="MergeRequest",
            discussion_id=DISCUSSION_ID,
        ),
        "merge_request": NoteMergeRequest(
            iid=MR_IID,
            title=MR_TITLE,
            source_branch=SOURCE_BRANCH,
            target_branch=TARGET_BRANCH,
        ),
    }
    return NoteWebhookPayload(**(defaults | overrides))


def _make_agent() -> AgentIdentity:
    return AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)


def _make_discussion_note(
    note_id: int = NOTE_ID,
    author_id: int = 1,
    author_username: str = "developer",
    body: str = NOTE_TEXT,
) -> DiscussionNote:
    return DiscussionNote(
        note_id=note_id,
        author_id=author_id,
        author_username=author_username,
        body=body,
        created_at="2024-01-15T10:00:00Z",
        is_system=False,
        resolved=None,
        resolvable=True,
        position=None,
    )


def _make_discussion(
    discussion_id: str = DISCUSSION_ID,
    notes: list[DiscussionNote] | None = None,
) -> Discussion:
    return Discussion(
        discussion_id=discussion_id,
        notes=notes or [_make_discussion_note()],
        is_resolved=False,
        is_inline=False,
    )


def _gl_thread_mock() -> MagicMock:
    """Build a mock GitLab discussion object with notes.create()."""
    disc_obj = MagicMock()
    disc_obj.notes.create = MagicMock()
    return disc_obj


def _wire_gitlab_sdk(mock_gitlab_cls: MagicMock, disc_obj: MagicMock) -> None:
    """Wire mock gitlab.Gitlab → project → MR → discussions.get → disc_obj."""
    gl_instance = MagicMock()
    mock_gitlab_cls.return_value = gl_instance
    gl_project = MagicMock()
    gl_instance.projects.get.return_value = gl_project
    gl_mr = MagicMock()
    gl_project.mergerequests.get.return_value = gl_mr
    gl_mr.discussions.get.return_value = disc_obj


# -- Tests --


@patch(f"{_MOD}.gitlab.Gitlab")
@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
@patch(f"{_MOD}.GitLabClient")
async def test_qa_reply_no_code_changes(
    mock_client_cls: MagicMock,
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    mock_gl_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """Happy path: Q&A reply with no code changes — reply posted, no push."""
    from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction

    # GitLabClient instance
    gl_client = AsyncMock()
    mock_client_cls.return_value = gl_client
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    # LLM returns a ReviewResult (no patch)
    mock_run.return_value = ReviewResult(summary=REPLY_TEXT)
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(reply=REPLY_TEXT)

    # Wire gitlab SDK for thread reply
    disc_obj = _gl_thread_mock()
    _wire_gitlab_sdk(mock_gl_cls, disc_obj)

    executor = AsyncMock()
    await handle_discussion_interaction(
        make_settings(),
        _make_payload(),
        executor,
        _make_agent(),
    )

    # Reply posted to correct thread
    disc_obj.notes.create.assert_called_once()
    posted_body = disc_obj.notes.create.call_args[0][0]["body"]
    assert REPLY_TEXT in posted_body

    # No git push attempted (ReviewResult has no patch)
    gl_client.clone_repo.assert_awaited_once()


@patch(f"{_MOD}.git_push")
@patch(f"{_MOD}.git_commit")
@patch(f"{_MOD}.apply_coding_result")
@patch(f"{_MOD}.gitlab.Gitlab")
@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
@patch(f"{_MOD}.GitLabClient")
async def test_coding_reply_with_changes(
    mock_client_cls: MagicMock,
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    mock_gl_cls: MagicMock,
    mock_apply: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    tmp_path: Path,
) -> None:
    """Happy path: coding reply with patch — changes pushed."""
    from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction

    gl_client = AsyncMock()
    mock_client_cls.return_value = gl_client
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_run.return_value = CodingResult(summary=REPLY_TEXT, patch="diff --git a/f b/f")
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(reply=REPLY_TEXT)
    mock_commit.return_value = True

    disc_obj = _gl_thread_mock()
    _wire_gitlab_sdk(mock_gl_cls, disc_obj)

    executor = AsyncMock()
    await handle_discussion_interaction(
        make_settings(),
        _make_payload(),
        executor,
        _make_agent(),
    )

    mock_apply.assert_awaited_once()
    mock_commit.assert_awaited_once()
    mock_push.assert_awaited_once()

    posted_body = disc_obj.notes.create.call_args[0][0]["body"]
    assert CHANGES_PUSHED_MARKER in posted_body


@patch(f"{_MOD}.git_push")
@patch(f"{_MOD}.git_commit")
@patch(f"{_MOD}.apply_coding_result")
@patch(f"{_MOD}.gitlab.Gitlab")
@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
@patch(f"{_MOD}.GitLabClient")
async def test_coding_reply_empty_patch(
    mock_client_cls: MagicMock,
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    mock_gl_cls: MagicMock,
    mock_apply: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    tmp_path: Path,
) -> None:
    """Coding result with empty patch — reply posted, no push."""
    from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction

    gl_client = AsyncMock()
    mock_client_cls.return_value = gl_client
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_run.return_value = CodingResult(summary=REPLY_TEXT, patch="")
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(reply=REPLY_TEXT)

    disc_obj = _gl_thread_mock()
    _wire_gitlab_sdk(mock_gl_cls, disc_obj)

    executor = AsyncMock()
    await handle_discussion_interaction(
        make_settings(),
        _make_payload(),
        executor,
        _make_agent(),
    )

    mock_apply.assert_not_awaited()
    mock_push.assert_not_awaited()

    posted_body = disc_obj.notes.create.call_args[0][0]["body"]
    assert CHANGES_PUSHED_MARKER not in posted_body


@patch(f"{_MOD}.GitLabClient")
async def test_triggering_discussion_not_found(
    mock_client_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """When the triggering note is not in any discussion, handler returns without posting."""
    from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction

    gl_client = AsyncMock()
    mock_client_cls.return_value = gl_client
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()

    # Return a discussion with a different note_id
    other_note = _make_discussion_note(note_id=999)
    gl_client.list_mr_discussions.return_value = [_make_discussion(notes=[other_note])]

    executor = AsyncMock()
    await handle_discussion_interaction(
        make_settings(),
        _make_payload(),
        executor,
        _make_agent(),
    )

    # No LLM call, no reply posted
    executor.execute.assert_not_awaited()


@patch(f"{_MOD}.GitLabClient")
@patch(f"{_MOD}.run_discussion")
@patch(f"{_MOD}.build_discussion_prompt")
async def test_task_execution_error_posts_user_error(
    mock_build: MagicMock,
    mock_run: AsyncMock,
    mock_client_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """TaskExecutionError → user-friendly error comment on MR, exception re-raised."""
    from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction

    gl_client = AsyncMock()
    mock_client_cls.return_value = gl_client
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_build.return_value = "prompt"
    mock_run.side_effect = TaskExecutionError("authentication failed")

    executor = AsyncMock()
    with pytest.raises(TaskExecutionError):
        await handle_discussion_interaction(
            make_settings(),
            _make_payload(),
            executor,
            _make_agent(),
        )

    gl_client.post_mr_comment.assert_awaited_once()
    posted_body = gl_client.post_mr_comment.call_args[0][2]
    assert "❌" in posted_body


@patch(f"{_MOD}.GitLabClient")
async def test_general_exception_posts_generic_error(
    mock_client_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """General exception → generic error comment on MR, exception re-raised."""
    from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction

    gl_client = AsyncMock()
    mock_client_cls.return_value = gl_client
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.side_effect = RuntimeError("unexpected")

    executor = AsyncMock()
    with pytest.raises(RuntimeError, match="unexpected"):
        await handle_discussion_interaction(
            make_settings(),
            _make_payload(),
            executor,
            _make_agent(),
        )

    gl_client.post_mr_comment.assert_awaited_once()
    posted_body = gl_client.post_mr_comment.call_args[0][2]
    assert GENERIC_ERROR_SNIPPET in posted_body


@patch(f"{_MOD}.shutil.rmtree")
@patch(f"{_MOD}.GitLabClient")
async def test_cleanup_runs_on_failure(
    mock_client_cls: MagicMock,
    mock_rmtree: MagicMock,
    tmp_path: Path,
) -> None:
    """Repo cleanup runs even when the handler raises."""
    from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction

    gl_client = AsyncMock()
    mock_client_cls.return_value = gl_client
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.side_effect = RuntimeError("boom")

    executor = AsyncMock()
    with pytest.raises(RuntimeError):
        await handle_discussion_interaction(
            make_settings(),
            _make_payload(),
            executor,
            _make_agent(),
        )

    # shutil.rmtree called via asyncio.to_thread — the mock captures the call
    mock_rmtree.assert_called_once_with(tmp_path, True)


@patch(f"{_MOD}.gitlab.Gitlab")
@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
@patch(f"{_MOD}.GitLabClient")
async def test_repo_lock_used_when_provided(
    mock_client_cls: MagicMock,
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    mock_gl_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """When repo_locks is provided, _execute runs inside the lock."""
    from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction

    gl_client = AsyncMock()
    mock_client_cls.return_value = gl_client
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_run.return_value = ReviewResult(summary=REPLY_TEXT)
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(reply=REPLY_TEXT)

    disc_obj = _gl_thread_mock()
    _wire_gitlab_sdk(mock_gl_cls, disc_obj)

    # Build a mock DistributedLock with async context manager
    lock_cm = AsyncMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=False)

    repo_locks = MagicMock()
    repo_locks.acquire.return_value = lock_cm

    executor = AsyncMock()
    await handle_discussion_interaction(
        make_settings(),
        _make_payload(),
        executor,
        _make_agent(),
        repo_locks=repo_locks,
    )

    repo_locks.acquire.assert_called_once_with(CLONE_URL)
    lock_cm.__aenter__.assert_awaited_once()
    lock_cm.__aexit__.assert_awaited_once()

    # Verify the handler still completed (reply posted)
    disc_obj.notes.create.assert_called_once()


@patch(f"{_MOD}.gitlab.Gitlab")
@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
@patch(f"{_MOD}.GitLabClient")
async def test_deleted_branch_replies_with_warning(
    mock_client_cls: MagicMock,
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    mock_gl_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """When the source branch is deleted, reply with a helpful warning."""
    from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction

    gl_client = AsyncMock()
    mock_client_cls.return_value = gl_client
    gl_client.clone_repo = AsyncMock(
        side_effect=RuntimeError(
            "git clone failed: Remote branch feature/test not found in upstream origin"
        )
    )
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    disc_obj = MagicMock()
    disc_obj.notes.create = MagicMock()
    gl_mr_mock = mock_gl_cls.return_value.projects.get.return_value.mergerequests.get.return_value
    gl_mr_mock.discussions.get.return_value = disc_obj

    await handle_discussion_interaction(
        make_settings(),
        _make_payload(),
        AsyncMock(),
        _make_agent(),
    )

    # LLM should NOT have been called
    mock_run.assert_not_awaited()

    # Warning reply posted to thread
    disc_obj.notes.create.assert_called_once()
    reply = disc_obj.notes.create.call_args[0][0]["body"]
    assert "deleted" in reply or "inaccessible" in reply
    assert SOURCE_BRANCH in reply
