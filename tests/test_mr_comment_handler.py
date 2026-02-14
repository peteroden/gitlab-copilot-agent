"""Tests for the MR /copilot comment handler."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

from gitlab_copilot_agent.mr_comment_handler import handle_copilot_comment, parse_copilot_command
from gitlab_copilot_agent.models import (
    NoteWebhookPayload, NoteObjectAttributes, NoteMergeRequest,
    WebhookProject, WebhookUser,
)
from tests.conftest import make_settings


def test_parse_copilot_command_valid() -> None:
    assert parse_copilot_command("/copilot fix the null check") == "fix the null check"


def test_parse_copilot_command_case_insensitive() -> None:
    assert parse_copilot_command("/Copilot add tests") == "add tests"


def test_parse_copilot_command_not_a_command() -> None:
    assert parse_copilot_command("just a regular comment") is None


def test_parse_copilot_command_empty_instruction() -> None:
    assert parse_copilot_command("/copilot ") is None


def _make_note_payload(note: str = "/copilot fix bug") -> NoteWebhookPayload:
    return NoteWebhookPayload(
        object_kind="note",
        user=WebhookUser(id=1, username="reviewer"),
        project=WebhookProject(id=42, path_with_namespace="g/p", git_http_url="https://gl.example.com/g/p.git"),
        object_attributes=NoteObjectAttributes(note=note, noteable_type="MergeRequest"),
        merge_request=NoteMergeRequest(iid=7, title="Fix", source_branch="feature/x", target_branch="main"),
    )


@patch("gitlab_copilot_agent.mr_comment_handler.GitLabClient")
@patch("gitlab_copilot_agent.mr_comment_handler.run_copilot_session")
@patch("gitlab_copilot_agent.mr_comment_handler.git_push")
@patch("gitlab_copilot_agent.mr_comment_handler.git_commit")
@patch("gitlab_copilot_agent.mr_comment_handler.git_clone")
async def test_handle_full_pipeline(
    mock_clone: AsyncMock, mock_commit: AsyncMock, mock_push: AsyncMock,
    mock_session: AsyncMock, mock_gl_class: AsyncMock, tmp_path: Path,
) -> None:
    mock_clone.return_value = tmp_path
    mock_session.return_value = "Fixed the bug"
    mock_commit.return_value = True
    mock_gl = AsyncMock()
    mock_gl_class.return_value = mock_gl

    await handle_copilot_comment(make_settings(), _make_note_payload())

    mock_clone.assert_awaited_once()
    mock_session.assert_awaited_once()
    mock_push.assert_awaited_once()
    mock_gl.post_mr_comment.assert_awaited_once()
