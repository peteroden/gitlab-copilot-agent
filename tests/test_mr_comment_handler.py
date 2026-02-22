"""Tests for the MR /copilot comment handler."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

from gitlab_copilot_agent.approval_store import MemoryApprovalStore
from gitlab_copilot_agent.models import (
    NoteMergeRequest,
    NoteObjectAttributes,
    NoteWebhookPayload,
    PendingApproval,
    WebhookProject,
    WebhookUser,
)
from gitlab_copilot_agent.mr_comment_handler import (
    handle_copilot_comment,
    is_approval_command,
    parse_copilot_command,
)
from gitlab_copilot_agent.task_executor import CodingResult
from tests.conftest import MR_IID, PROJECT_ID, make_settings


def test_parse_copilot_command_valid() -> None:
    assert parse_copilot_command("/copilot fix the null check") == "fix the null check"


def test_parse_copilot_command_case_insensitive() -> None:
    assert parse_copilot_command("/Copilot add tests") == "add tests"


def test_parse_copilot_command_not_a_command() -> None:
    assert parse_copilot_command("just a regular comment") is None


def test_parse_copilot_command_empty_instruction() -> None:
    assert parse_copilot_command("/copilot ") is None


def test_parse_copilot_command_approve_returns_none() -> None:
    """Approval commands should not be parsed as regular commands."""
    assert parse_copilot_command("/copilot approve") is None


def test_is_approval_command() -> None:
    assert is_approval_command("/copilot approve") is True
    assert is_approval_command("/Copilot Approve") is True
    assert is_approval_command("/copilot approve ") is True
    assert is_approval_command("/copilot fix bug") is False
    assert is_approval_command("regular comment") is False


def _make_note_payload(
    note: str = "/copilot fix bug", user_id: int = 1, username: str = "reviewer"
) -> NoteWebhookPayload:
    return NoteWebhookPayload(
        object_kind="note",
        user=WebhookUser(id=user_id, username=username),
        project=WebhookProject(
            id=PROJECT_ID, path_with_namespace="g/p", git_http_url="https://gl.example.com/g/p.git"
        ),
        object_attributes=NoteObjectAttributes(note=note, noteable_type="MergeRequest"),
        merge_request=NoteMergeRequest(
            iid=MR_IID, title="Fix", source_branch="feature/x", target_branch="main"
        ),
    )


@patch("gitlab_copilot_agent.mr_comment_handler.GitLabClient")
@patch("gitlab_copilot_agent.mr_comment_handler.git_push")
@patch("gitlab_copilot_agent.mr_comment_handler.git_commit")
@patch("gitlab_copilot_agent.mr_comment_handler.git_clone")
async def test_handle_full_pipeline(
    mock_clone: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    mock_gl_class: AsyncMock,
    tmp_path: Path,
) -> None:
    """Test full pipeline without approval required."""
    mock_clone.return_value = tmp_path
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = CodingResult(summary="Fixed the bug")
    mock_commit.return_value = True
    mock_gl = AsyncMock()
    mock_gl_class.return_value = mock_gl

    await handle_copilot_comment(make_settings(), _make_note_payload(), executor=mock_executor)

    mock_clone.assert_awaited_once()
    mock_executor.execute.assert_awaited_once()
    mock_push.assert_awaited_once()
    mock_gl.post_mr_comment.assert_awaited_once()


@patch("gitlab_copilot_agent.mr_comment_handler.GitLabClient")
async def test_handle_approval_required(mock_gl_class: AsyncMock) -> None:
    """Test that approval requirement stores pending approval and posts confirmation."""
    mock_gl = AsyncMock()
    mock_gl_class.return_value = mock_gl
    mock_executor = AsyncMock()
    approval_store = MemoryApprovalStore()

    settings = make_settings(copilot_require_approval=True, copilot_approval_timeout=3600)
    payload = _make_note_payload(note="/copilot fix the bug", user_id=123)

    await handle_copilot_comment(
        settings, payload, executor=mock_executor, approval_store=approval_store
    )

    # Should NOT execute
    mock_executor.execute.assert_not_awaited()

    # Should post confirmation comment
    mock_gl.post_mr_comment.assert_awaited_once()
    call_args = mock_gl.post_mr_comment.await_args
    assert call_args[0][0] == PROJECT_ID
    assert call_args[0][1] == MR_IID
    assert "⏳ Approval required" in call_args[0][2]

    # Should store pending approval
    pending = await approval_store.pop(PROJECT_ID, MR_IID)
    assert pending is not None
    assert pending.requester_id == 123
    assert pending.prompt == "fix the bug"


@patch("gitlab_copilot_agent.mr_comment_handler.GitLabClient")
@patch("gitlab_copilot_agent.mr_comment_handler.git_push")
@patch("gitlab_copilot_agent.mr_comment_handler.git_commit")
@patch("gitlab_copilot_agent.mr_comment_handler.git_clone")
async def test_handle_approval_command_success(
    mock_clone: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    mock_gl_class: AsyncMock,
    tmp_path: Path,
) -> None:
    """Test /copilot approve executes the pending command."""
    mock_clone.return_value = tmp_path
    mock_commit.return_value = True
    mock_gl = AsyncMock()
    mock_gl_class.return_value = mock_gl
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = CodingResult(summary="Done")
    approval_store = MemoryApprovalStore()

    # Store a pending approval
    await approval_store.store(
        PendingApproval(
            task_id=f"mr-{PROJECT_ID}-{MR_IID}",
            requester_id=123,
            prompt="fix the bug",
            mr_iid=MR_IID,
            project_id=PROJECT_ID,
            timeout=3600,
        )
    )

    # User approves
    settings = make_settings()
    payload = _make_note_payload(note="/copilot approve", user_id=123)

    await handle_copilot_comment(
        settings, payload, executor=mock_executor, approval_store=approval_store
    )

    # Should execute
    mock_executor.execute.assert_awaited_once()
    mock_clone.assert_awaited_once()
    mock_push.assert_awaited_once()

    # Should clear pending approval
    assert await approval_store.pop(PROJECT_ID, MR_IID) is None


@patch("gitlab_copilot_agent.mr_comment_handler.GitLabClient")
async def test_handle_approval_command_wrong_user(mock_gl_class: AsyncMock) -> None:
    """Test that only the requester can approve."""
    mock_gl = AsyncMock()
    mock_gl_class.return_value = mock_gl
    mock_executor = AsyncMock()
    approval_store = MemoryApprovalStore()

    # Store pending approval for user 123
    await approval_store.store(
        PendingApproval(
            task_id=f"mr-{PROJECT_ID}-{MR_IID}",
            requester_id=123,
            prompt="fix the bug",
            mr_iid=MR_IID,
            project_id=PROJECT_ID,
            timeout=3600,
        )
    )

    # Different user tries to approve
    settings = make_settings()
    payload = _make_note_payload(note="/copilot approve", user_id=999)

    await handle_copilot_comment(
        settings, payload, executor=mock_executor, approval_store=approval_store
    )

    # Should NOT execute
    mock_executor.execute.assert_not_awaited()

    # Should post error comment
    mock_gl.post_mr_comment.assert_awaited_once()
    call_args = mock_gl.post_mr_comment.await_args
    assert "❌ Only the original requester can approve" in call_args[0][2]

    # Pending approval should still exist (re-stored after wrong-user rejection)
    assert await approval_store.pop(PROJECT_ID, MR_IID) is not None


@patch("gitlab_copilot_agent.mr_comment_handler.GitLabClient")
async def test_handle_approval_command_no_pending(mock_gl_class: AsyncMock) -> None:
    """Test /copilot approve when there's no pending approval is a no-op."""
    mock_gl = AsyncMock()
    mock_gl_class.return_value = mock_gl
    mock_executor = AsyncMock()
    approval_store = MemoryApprovalStore()

    settings = make_settings()
    payload = _make_note_payload(note="/copilot approve", user_id=123)

    await handle_copilot_comment(
        settings, payload, executor=mock_executor, approval_store=approval_store
    )

    # Should NOT execute or post any comment
    mock_executor.execute.assert_not_awaited()
    mock_gl.post_mr_comment.assert_not_awaited()
