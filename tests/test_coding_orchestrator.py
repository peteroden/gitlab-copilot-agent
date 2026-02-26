"""Tests for the coding orchestrator."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.coding_orchestrator import CodingOrchestrator
from gitlab_copilot_agent.git_operations import TransientCloneError
from gitlab_copilot_agent.jira_models import JiraIssue, JiraIssueFields, JiraStatus
from gitlab_copilot_agent.project_mapping import GitLabProjectMapping
from gitlab_copilot_agent.task_executor import CodingResult
from tests.conftest import (
    EXAMPLE_CLONE_URL,
    JIRA_EMAIL,
    JIRA_TOKEN,
    JIRA_URL,
    make_settings,
)

_JIRA_SETTINGS = {
    "jira_url": JIRA_URL,
    "jira_email": JIRA_EMAIL,
    "jira_api_token": JIRA_TOKEN,
    "jira_project_map": '{"mappings": {}}',
}

_TEST_ISSUE = JiraIssue(
    id="10042",
    key="PROJ-42",
    fields=JiraIssueFields(
        summary="Add feature",
        status=JiraStatus(name="AI Ready", id="1"),
        description="Impl",
    ),
)

_TEST_MAPPING = GitLabProjectMapping(
    gitlab_project_id=99,
    clone_url=EXAMPLE_CLONE_URL,
    target_branch="main",
)


@patch("gitlab_copilot_agent.coding_orchestrator.run_coding_task")
@patch("gitlab_copilot_agent.coding_orchestrator.git_push")
@patch("gitlab_copilot_agent.coding_orchestrator.git_commit")
@patch("gitlab_copilot_agent.coding_orchestrator.git_unique_branch")
@patch("gitlab_copilot_agent.coding_orchestrator.git_clone")
async def test_handle_full_pipeline(
    mock_clone: AsyncMock,
    mock_branch: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    mock_coding: AsyncMock,
    tmp_path: Path,
) -> None:
    mock_clone.return_value = tmp_path
    mock_branch.return_value = "agent/proj-42"
    mock_coding.return_value = CodingResult(summary="Changes made")
    mock_gitlab, mock_jira = AsyncMock(), AsyncMock()
    mock_gitlab.create_merge_request.return_value = 1
    orch = CodingOrchestrator(make_settings(**_JIRA_SETTINGS), mock_gitlab, mock_jira, AsyncMock())
    await orch.handle(_TEST_ISSUE, _TEST_MAPPING)
    mock_clone.assert_awaited_once()
    mock_branch.assert_awaited_once_with(tmp_path, "agent/proj-42")
    mock_commit.assert_awaited_once()
    mock_push.assert_awaited_once()
    mock_gitlab.create_merge_request.assert_awaited_once()
    assert mock_jira.transition_issue.await_count == 2
    mock_jira.transition_issue.assert_any_await("PROJ-42", "In Progress")
    mock_jira.transition_issue.assert_any_await("PROJ-42", "In Review")
    mock_jira.add_comment.assert_awaited_once()


@patch("gitlab_copilot_agent.coding_orchestrator.run_coding_task")
@patch("gitlab_copilot_agent.coding_orchestrator.git_push")
@patch("gitlab_copilot_agent.coding_orchestrator.git_commit")
@patch("gitlab_copilot_agent.coding_orchestrator.git_unique_branch")
@patch("gitlab_copilot_agent.coding_orchestrator.git_clone")
async def test_in_review_transition_failure_is_non_blocking(
    mock_clone: AsyncMock,
    mock_branch: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    mock_coding: AsyncMock,
    tmp_path: Path,
) -> None:
    """If 'In Review' transition fails, the task still completes successfully."""
    mock_clone.return_value = tmp_path
    mock_branch.return_value = "agent/proj-42"
    mock_coding.return_value = CodingResult(summary="Changes made")
    mock_gitlab, mock_jira = AsyncMock(), AsyncMock()
    mock_gitlab.create_merge_request.return_value = 1

    # First call (In Progress) succeeds, second call (In Review) fails
    mock_jira.transition_issue.side_effect = [None, ValueError("No transition")]

    orch = CodingOrchestrator(make_settings(**_JIRA_SETTINGS), mock_gitlab, mock_jira, AsyncMock())
    await orch.handle(_TEST_ISSUE, _TEST_MAPPING)

    assert mock_jira.transition_issue.await_count == 2
    mock_gitlab.create_merge_request.assert_awaited_once()
    mock_jira.add_comment.assert_awaited_once()


@patch("gitlab_copilot_agent.coding_orchestrator.run_coding_task")
@patch("gitlab_copilot_agent.coding_orchestrator.git_push")
@patch("gitlab_copilot_agent.coding_orchestrator.git_commit")
@patch("gitlab_copilot_agent.coding_orchestrator.git_unique_branch")
@patch("gitlab_copilot_agent.coding_orchestrator.git_clone")
async def test_custom_in_review_status_used(
    mock_clone: AsyncMock,
    mock_branch: AsyncMock,
    mock_commit: AsyncMock,
    mock_push: AsyncMock,
    mock_coding: AsyncMock,
    tmp_path: Path,
) -> None:
    """Custom JIRA_IN_REVIEW_STATUS is used for the transition."""
    mock_clone.return_value = tmp_path
    mock_branch.return_value = "agent/proj-42"
    mock_coding.return_value = CodingResult(summary="Changes made")
    mock_gitlab, mock_jira = AsyncMock(), AsyncMock()
    mock_gitlab.create_merge_request.return_value = 1

    settings = make_settings(**_JIRA_SETTINGS, jira_in_review_status="QA Review")
    orch = CodingOrchestrator(settings, mock_gitlab, mock_jira, AsyncMock())
    await orch.handle(_TEST_ISSUE, _TEST_MAPPING)

    mock_jira.transition_issue.assert_any_await("PROJ-42", "QA Review")


@patch("gitlab_copilot_agent.coding_orchestrator.git_clone")
async def test_coding_failure_posts_comment_to_jira(
    mock_clone: AsyncMock,
) -> None:
    """Verify that coding task failures post a comment to Jira."""
    mock_clone.side_effect = Exception("Git clone failed")
    mock_gitlab, mock_jira = AsyncMock(), AsyncMock()
    orch = CodingOrchestrator(make_settings(**_JIRA_SETTINGS), mock_gitlab, mock_jira, AsyncMock())

    with pytest.raises(Exception, match="Git clone failed"):
        await orch.handle(_TEST_ISSUE, _TEST_MAPPING)

    mock_jira.add_comment.assert_awaited_once()
    call_args = mock_jira.add_comment.call_args
    assert call_args[0][0] == "PROJ-42"
    assert "Automated implementation failed" in call_args[0][1]


@patch("gitlab_copilot_agent.coding_orchestrator.git_clone")
async def test_coding_failure_comment_posting_failure_is_logged(
    mock_clone: AsyncMock,
) -> None:
    """If posting the failure comment itself fails, the original exception still raises."""
    mock_clone.side_effect = Exception("Git clone failed")
    mock_gitlab, mock_jira = AsyncMock(), AsyncMock()
    mock_jira.add_comment.side_effect = Exception("Jira API error")
    orch = CodingOrchestrator(make_settings(**_JIRA_SETTINGS), mock_gitlab, mock_jira, AsyncMock())

    with pytest.raises(Exception, match="Git clone failed"):
        await orch.handle(_TEST_ISSUE, _TEST_MAPPING)

    mock_jira.add_comment.assert_awaited_once()


@patch("gitlab_copilot_agent.coding_orchestrator.git_clone")
async def test_transient_clone_failure_posts_detailed_comment(
    mock_clone: AsyncMock,
) -> None:
    """Transient clone failure posts detailed comment and does not mark as processed."""
    mock_clone.side_effect = TransientCloneError(
        "git clone failed for https://gitlab.example.com/group/project.git after 3 attempts: "
        "The requested URL returned error: 403",
        attempts=3,
    )
    mock_gitlab, mock_jira = AsyncMock(), AsyncMock()
    orch = CodingOrchestrator(make_settings(**_JIRA_SETTINGS), mock_gitlab, mock_jira, AsyncMock())

    # Should NOT re-raise — transient failures are handled gracefully
    await orch.handle(_TEST_ISSUE, _TEST_MAPPING)

    # Verify Jira comment content
    assert mock_jira.add_comment.await_count == 1
    call_args = mock_jira.add_comment.call_args
    assert call_args[0][0] == "PROJ-42"
    comment = call_args[0][1]
    assert "3 attempts" in comment
    assert "transient error" in comment
    assert "retry on the next poll cycle" in comment

    # Issue should NOT be marked as processed — allow retry on next poll
    assert not orch._tracker.is_processed("PROJ-42")
