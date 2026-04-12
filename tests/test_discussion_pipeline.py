"""Tests for the discussion pipeline — DiscussionPipeline end-to-end."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from gitlab_copilot_agent.comment_parser import Resolution
from gitlab_copilot_agent.discussion_engine import DiscussionResponse
from gitlab_copilot_agent.discussion_models import (
    AgentIdentity,
    Discussion,
    DiscussionNote,
)
from gitlab_copilot_agent.discussion_pipeline import DiscussionContext, DiscussionPipeline
from gitlab_copilot_agent.events import TaskEvent
from gitlab_copilot_agent.pipeline import run_pipeline
from gitlab_copilot_agent.task_executor import CodingResult, ReviewResult, TaskExecutionError
from tests.conftest import GITLAB_TOKEN, MR_IID, PROJECT_ID, make_settings

# -- Test constants (discussion-specific) --

NOTE_ID = 501
DISCUSSION_ID = "disc-001"
AGENT_USER_ID = 99
AGENT_USERNAME = "review-bot"
SOURCE_BRANCH = "feature/test"
TARGET_BRANCH = "main"
CLONE_URL = "https://gitlab.example.com/group/project.git"
PROJECT_PATH = "group/project"
NOTE_TEXT = "@review-bot please explain this"
REPLY_TEXT = "Here is my explanation."
CHANGES_PUSHED_MARKER = "✅ Changes pushed."
GENERIC_ERROR_SNIPPET = "❌ Unable to process your request"

# -- Module path prefix for patches --
_MOD = "gitlab_copilot_agent.discussion_pipeline"


# -- Factories --


def _make_event(**overrides: Any) -> TaskEvent:
    defaults: dict[str, Any] = {
        "task_type": "discussion",
        "project_id": PROJECT_ID,
        "repo": PROJECT_PATH,
        "clone_url": CLONE_URL,
        "branch": SOURCE_BRANCH,
        "target_branch": TARGET_BRANCH,
        "mr_iid": MR_IID,
        "trigger_source": "webhook",
        "token": GITLAB_TOKEN,
        "note_id": NOTE_ID,
        "discussion_id": DISCUSSION_ID,
        "note_body": NOTE_TEXT,
    }
    return TaskEvent(**(defaults | overrides))


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
    is_inline: bool = False,
) -> Discussion:
    return Discussion(
        discussion_id=discussion_id,
        notes=notes or [_make_discussion_note()],
        is_resolved=False,
        is_inline=is_inline,
    )


# -- Tests --


@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
async def test_qa_reply_no_code_changes(
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    tmp_path: Path,
) -> None:
    """Happy path: Q&A reply with no code changes — reply posted, no push."""
    # GitLabClient instance
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    # LLM returns a ReviewResult (no patch)
    mock_run.return_value = ReviewResult(summary=REPLY_TEXT)
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(reply=REPLY_TEXT)

    executor = AsyncMock()
    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(),
        executor=executor,
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    # Reply posted to correct thread
    gl_client.reply_to_discussion.assert_awaited_once()
    posted_body = gl_client.reply_to_discussion.call_args[0][3]
    assert REPLY_TEXT in posted_body

    # No git push attempted (ReviewResult has no patch)
    gl_client.clone_repo.assert_awaited_once()


@patch(f"{_MOD}.git_push")
@patch(f"{_MOD}.git_commit")
@patch(f"{_MOD}.apply_coding_result")
@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
async def test_coding_reply_with_changes(
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    mock_apply: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    tmp_path: Path,
) -> None:
    """Happy path: coding reply with patch — changes pushed."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_run.return_value = CodingResult(summary=REPLY_TEXT, patch="diff --git a/f b/f")
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(reply=REPLY_TEXT)
    mock_commit.return_value = True

    executor = AsyncMock()
    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(),
        executor=executor,
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    mock_apply.assert_awaited_once()
    mock_commit.assert_awaited_once()
    mock_push.assert_awaited_once()

    posted_body = gl_client.reply_to_discussion.call_args[0][3]
    assert CHANGES_PUSHED_MARKER in posted_body


@patch(f"{_MOD}.git_push")
@patch(f"{_MOD}.git_commit")
@patch(f"{_MOD}.apply_coding_result")
@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
async def test_coding_reply_empty_patch(
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    mock_apply: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    tmp_path: Path,
) -> None:
    """Coding result with empty patch — reply posted, no push."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_run.return_value = CodingResult(summary=REPLY_TEXT, patch="")
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(reply=REPLY_TEXT)

    executor = AsyncMock()
    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(),
        executor=executor,
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    mock_apply.assert_not_awaited()
    mock_push.assert_not_awaited()

    posted_body = gl_client.reply_to_discussion.call_args[0][3]
    assert CHANGES_PUSHED_MARKER not in posted_body


async def test_triggering_discussion_not_found(
    tmp_path: Path,
) -> None:
    """When the triggering note is not in any discussion, handler returns without posting."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()

    # Return a discussion with a different note_id
    other_note = _make_discussion_note(note_id=999)
    gl_client.list_mr_discussions.return_value = [_make_discussion(notes=[other_note])]

    executor = AsyncMock()
    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(),
        executor=executor,
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    # No LLM call, no reply posted
    executor.execute.assert_not_awaited()


@patch(f"{_MOD}.run_discussion")
@patch(f"{_MOD}.build_discussion_prompt")
async def test_task_execution_error_posts_user_error(
    mock_build: MagicMock,
    mock_run: AsyncMock,
    tmp_path: Path,
) -> None:
    """TaskExecutionError → user-friendly error comment on MR, exception re-raised."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_build.return_value = "prompt"
    mock_run.side_effect = TaskExecutionError("authentication failed")

    executor = AsyncMock()
    with pytest.raises(TaskExecutionError):
        pipeline = DiscussionPipeline(
            settings=make_settings(),
            event=_make_event(),
            executor=executor,
            gl_client=gl_client,
            agent_identity=_make_agent(),
        )
        await run_pipeline(pipeline, DiscussionContext())

    gl_client.post_mr_comment.assert_awaited_once()
    posted_body = gl_client.post_mr_comment.call_args[0][2]
    assert "❌" in posted_body


async def test_general_exception_posts_generic_error(
    tmp_path: Path,
) -> None:
    """General exception → generic error comment on MR, exception re-raised."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.side_effect = RuntimeError("unexpected")

    executor = AsyncMock()
    with pytest.raises(RuntimeError, match="unexpected"):
        pipeline = DiscussionPipeline(
            settings=make_settings(),
            event=_make_event(),
            executor=executor,
            gl_client=gl_client,
            agent_identity=_make_agent(),
        )
        await run_pipeline(pipeline, DiscussionContext())

    gl_client.post_mr_comment.assert_awaited_once()
    posted_body = gl_client.post_mr_comment.call_args[0][2]
    assert GENERIC_ERROR_SNIPPET in posted_body


@patch(f"{_MOD}.shutil.rmtree")
async def test_cleanup_runs_on_failure(
    mock_rmtree: MagicMock,
    tmp_path: Path,
) -> None:
    """Repo cleanup runs even when the handler raises."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.side_effect = RuntimeError("boom")

    executor = AsyncMock()
    with pytest.raises(RuntimeError):
        pipeline = DiscussionPipeline(
            settings=make_settings(),
            event=_make_event(),
            executor=executor,
            gl_client=gl_client,
            agent_identity=_make_agent(),
        )
        await run_pipeline(pipeline, DiscussionContext())

    # shutil.rmtree called via asyncio.to_thread — the mock captures the call
    mock_rmtree.assert_called_once_with(tmp_path, True)


# Repo lock test removed — lock management moved to callers (gitlab_webhook.py, gitlab_poller.py)
# in Phase 6.2. Lock behavior is covered by test_webhook.py and test_gitlab_poller.py.


@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
async def test_deleted_branch_replies_with_warning(
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    tmp_path: Path,
) -> None:
    """When the source branch is deleted, reply with a helpful warning."""
    gl_client = AsyncMock()
    gl_client.clone_repo = AsyncMock(
        side_effect=RuntimeError(
            "git clone failed: Remote branch feature/test not found in upstream origin"
        )
    )
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(),
        executor=AsyncMock(),
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    # LLM should NOT have been called
    mock_run.assert_not_awaited()

    # Warning reply posted to thread
    gl_client.reply_to_discussion.assert_awaited_once()
    reply = gl_client.reply_to_discussion.call_args[0][3]
    assert "deleted" in reply or "inaccessible" in reply
    assert SOURCE_BRANCH in reply


# -- Resolution handling tests --

RESOLUTION_DISCUSSION_ID = DISCUSSION_ID
RESOLUTION_MESSAGE = "Fix confirmed."


def _make_resolution(
    discussion_id: str = RESOLUTION_DISCUSSION_ID,
    status: str = "resolved",
    message: str = RESOLUTION_MESSAGE,
) -> Resolution:
    return Resolution(discussion_id=discussion_id, status=status, message=message)


@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
async def test_discussion_interaction_auto_resolve(
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    tmp_path: Path,
) -> None:
    """resolution_behavior='auto-resolve' + resolved → thread resolved via API."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    # Triggering discussion must be agent-authored + inline for auto-resolve
    gl_client.list_mr_discussions.return_value = [
        _make_discussion(
            notes=[_make_discussion_note(author_id=AGENT_USER_ID)],
            is_inline=True,
        )
    ]

    mock_run.return_value = ReviewResult(summary=REPLY_TEXT)
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(
        reply=REPLY_TEXT,
        resolution=_make_resolution(status="resolved"),
    )

    executor = AsyncMock()
    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(resolution_behavior="auto-resolve"),
        executor=executor,
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    # Reply posted
    gl_client.reply_to_discussion.assert_awaited_once()
    # Thread resolved
    gl_client.resolve_discussion.assert_awaited_once()


@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
async def test_discussion_interaction_suggest_no_resolve(
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    tmp_path: Path,
) -> None:
    """resolution_behavior='suggest' + resolved → reply posted, thread NOT resolved."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_run.return_value = ReviewResult(summary=REPLY_TEXT)
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(
        reply=REPLY_TEXT,
        resolution=_make_resolution(status="resolved"),
    )

    executor = AsyncMock()
    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(resolution_behavior="suggest"),
        executor=executor,
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    # Reply posted
    gl_client.reply_to_discussion.assert_awaited_once()
    # Thread NOT resolved (suggest mode only posts reply)
    gl_client.resolve_discussion.assert_not_awaited()


@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
async def test_discussion_interaction_off_no_action(
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    tmp_path: Path,
) -> None:
    """resolution_behavior='off' → no resolution action at all."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_run.return_value = ReviewResult(summary=REPLY_TEXT)
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(
        reply=REPLY_TEXT,
        resolution=_make_resolution(status="resolved"),
    )

    executor = AsyncMock()
    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(resolution_behavior="off"),
        executor=executor,
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    # Reply still posted
    gl_client.reply_to_discussion.assert_awaited_once()
    # No resolution action
    gl_client.resolve_discussion.assert_not_awaited()


@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
async def test_discussion_interaction_partial_never_resolved(
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    tmp_path: Path,
) -> None:
    """Partial resolution → never auto-resolved regardless of behavior."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [_make_discussion()]

    mock_run.return_value = ReviewResult(summary=REPLY_TEXT)
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(
        reply=REPLY_TEXT,
        resolution=_make_resolution(status="partial"),
    )

    executor = AsyncMock()
    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(resolution_behavior="auto-resolve"),
        executor=executor,
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    # Reply posted
    gl_client.reply_to_discussion.assert_awaited_once()
    # Partial → never resolved
    gl_client.resolve_discussion.assert_not_awaited()


@patch(f"{_MOD}.parse_discussion_response")
@patch(f"{_MOD}.build_discussion_prompt")
@patch(f"{_MOD}.run_discussion")
async def test_discussion_interaction_resolution_error_logged(
    mock_run: AsyncMock,
    mock_build: MagicMock,
    mock_parse: MagicMock,
    tmp_path: Path,
) -> None:
    """Exception during resolution → logged, not raised."""
    gl_client = AsyncMock()
    gl_client.clone_repo.return_value = tmp_path
    gl_client.get_mr_details.return_value = MagicMock()
    gl_client.list_mr_discussions.return_value = [
        _make_discussion(
            notes=[_make_discussion_note(author_id=AGENT_USER_ID)],
            is_inline=True,
        )
    ]

    mock_run.return_value = ReviewResult(summary=REPLY_TEXT)
    mock_build.return_value = "prompt"
    mock_parse.return_value = DiscussionResponse(
        reply=REPLY_TEXT,
        resolution=_make_resolution(status="resolved"),
    )

    # Make resolve_discussion raise to simulate resolution failure
    gl_client.resolve_discussion.side_effect = RuntimeError("resolve boom")

    executor = AsyncMock()
    # Should NOT raise — resolution errors are swallowed and logged
    pipeline = DiscussionPipeline(
        settings=make_settings(),
        event=_make_event(resolution_behavior="auto-resolve"),
        executor=executor,
        gl_client=gl_client,
        agent_identity=_make_agent(),
    )
    await run_pipeline(pipeline, DiscussionContext())

    # Reply still posted successfully
    gl_client.reply_to_discussion.assert_awaited_once()
    # resolve_discussion was attempted
    gl_client.resolve_discussion.assert_awaited_once()
