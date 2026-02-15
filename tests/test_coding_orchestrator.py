"""Tests for the coding orchestrator."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

from gitlab_copilot_agent.coding_orchestrator import CodingOrchestrator
from gitlab_copilot_agent.jira_models import JiraIssue, JiraIssueFields, JiraStatus
from gitlab_copilot_agent.project_mapping import GitLabProjectMapping
from tests.conftest import EXAMPLE_CLONE_URL, make_settings


@patch("gitlab_copilot_agent.coding_orchestrator.run_coding_task")
@patch("gitlab_copilot_agent.coding_orchestrator.git_push")
@patch("gitlab_copilot_agent.coding_orchestrator.git_commit")
@patch("gitlab_copilot_agent.coding_orchestrator.git_create_branch")
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
    mock_coding.return_value = "Changes made"
    settings = make_settings(
        jira_url="https://jira.example.com",
        jira_email="bot@example.com",
        jira_api_token="token",
        jira_project_map='{"mappings": {}}',
    )
    mock_gitlab, mock_jira = AsyncMock(), AsyncMock()
    mock_gitlab.create_merge_request.return_value = 1
    issue = JiraIssue(
        id="10042",
        key="PROJ-42",
        fields=JiraIssueFields(
            summary="Add feature", status=JiraStatus(name="AI Ready", id="1"), description="Impl"
        ),
    )
    mapping = GitLabProjectMapping(
        gitlab_project_id=99, clone_url=EXAMPLE_CLONE_URL, target_branch="main"
    )
    orch = CodingOrchestrator(settings, mock_gitlab, mock_jira)
    await orch.handle(issue, mapping)
    mock_clone.assert_awaited_once()
    mock_branch.assert_awaited_once_with(tmp_path, "agent/proj-42")
    mock_commit.assert_awaited_once()
    mock_push.assert_awaited_once()
    mock_gitlab.create_merge_request.assert_awaited_once()
    mock_jira.transition_issue.assert_awaited_once()
    mock_jira.add_comment.assert_awaited_once()
