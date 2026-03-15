"""Tests for the MR /copilot comment handler."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

from gitlab_copilot_agent.models import (
    NoteMergeRequest,
    NoteObjectAttributes,
    NoteWebhookPayload,
    WebhookProject,
    WebhookUser,
)
from gitlab_copilot_agent.mr_comment_handler import handle_copilot_comment, parse_copilot_command
from gitlab_copilot_agent.task_executor import CodingResult
from tests.conftest import GITLAB_TOKEN, MR_IID, PROJECT_ID, make_settings

PER_PROJECT_TOKEN = "project-specific-token"


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
@patch("gitlab_copilot_agent.mr_comment_handler.git_push")
@patch("gitlab_copilot_agent.mr_comment_handler.git_commit")
@patch("gitlab_copilot_agent.mr_comment_handler.git_clone")
async def test_handle_uses_per_project_token(
    mock_clone: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    mock_gl_class: AsyncMock,
    tmp_path: Path,
) -> None:
    """When project_token is passed, it is used instead of settings.gitlab_token."""
    mock_clone.return_value = tmp_path
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = CodingResult(summary="done")
    mock_commit.return_value = True
    mock_gl = AsyncMock()
    mock_gl_class.return_value = mock_gl

    await handle_copilot_comment(
        make_settings(),
        _make_note_payload(),
        executor=mock_executor,
        project_token=PER_PROJECT_TOKEN,
    )

    # GitLabClient created with per-project token
    mock_gl_class.assert_called_once()
    _, gl_args = mock_gl_class.call_args
    assert mock_gl_class.call_args[0][1] == PER_PROJECT_TOKEN
    # git_clone called with per-project token
    clone_args = mock_clone.call_args
    assert clone_args[0][2] == PER_PROJECT_TOKEN
    # git_push called with per-project token
    push_args = mock_push.call_args
    assert push_args[0][3] == PER_PROJECT_TOKEN


@patch("gitlab_copilot_agent.mr_comment_handler.GitLabClient")
@patch("gitlab_copilot_agent.mr_comment_handler.git_push")
@patch("gitlab_copilot_agent.mr_comment_handler.git_commit")
@patch("gitlab_copilot_agent.mr_comment_handler.git_clone")
async def test_handle_falls_back_to_settings_token(
    mock_clone: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    mock_gl_class: AsyncMock,
    tmp_path: Path,
) -> None:
    """When project_token is None, falls back to settings.gitlab_token."""
    mock_clone.return_value = tmp_path
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = CodingResult(summary="done")
    mock_commit.return_value = True
    mock_gl = AsyncMock()
    mock_gl_class.return_value = mock_gl

    await handle_copilot_comment(
        make_settings(),
        _make_note_payload(),
        executor=mock_executor,
        project_token=None,
    )

    # GitLabClient created with global token from settings
    mock_gl_class.assert_called_once()
    assert mock_gl_class.call_args[0][1] == GITLAB_TOKEN
