"""Tests for Jira poller."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gitlab_copilot_agent.config import JiraSettings
from gitlab_copilot_agent.jira_models import JiraIssue, JiraIssueFields, JiraStatus
from gitlab_copilot_agent.jira_poller import JiraPoller
from gitlab_copilot_agent.project_mapping import GitLabProjectMapping, ProjectMap
from tests.conftest import JIRA_EMAIL, JIRA_TOKEN, JIRA_URL


def make_jira_settings(**overrides: str | int) -> JiraSettings:
    """Create test JiraSettings with defaults."""
    defaults = {
        "url": JIRA_URL,
        "email": JIRA_EMAIL,
        "api_token": JIRA_TOKEN,
        "trigger_status": "AI Ready",
        "in_progress_status": "In Progress",
        "poll_interval": 1,  # Short interval for tests
        "project_map_json": '{"mappings": {}}',
    }
    return JiraSettings(**(defaults | overrides))  # type: ignore[arg-type]


def make_jira_issue(key: str = "PROJ-123", status: str = "AI Ready") -> JiraIssue:
    """Create a test JiraIssue."""
    return JiraIssue(
        id="10001",
        key=key,
        fields=JiraIssueFields(
            summary="Test issue",
            description="Test description",
            status=JiraStatus(name=status, id="1"),
            assignee=None,
            labels=[],
        ),
    )


@pytest.fixture
def project_map() -> ProjectMap:
    """Project map with a single test mapping."""
    return ProjectMap(
        mappings={
            "PROJ": GitLabProjectMapping(
                gitlab_project_id=42,
                clone_url="https://gitlab.example.com/group/project.git",
                target_branch="main",
            )
        }
    )


@pytest.fixture
def mock_jira_client() -> AsyncMock:
    """Mock JiraClient."""
    client = AsyncMock()
    client.search_issues = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_handler() -> AsyncMock:
    """Mock CodingTaskHandler."""
    handler = AsyncMock()
    handler.handle = AsyncMock()
    return handler


@pytest.mark.asyncio
async def test_poll_once_discovers_issues_and_calls_handler(
    mock_jira_client: AsyncMock,
    project_map: ProjectMap,
    mock_handler: AsyncMock,
) -> None:
    """Test that _poll_once discovers issues and invokes the handler."""
    issue = make_jira_issue("PROJ-123")
    mock_jira_client.search_issues.return_value = [issue]

    settings = make_jira_settings()
    poller = JiraPoller(mock_jira_client, settings, project_map, mock_handler)

    await poller._poll_once()

    # Verify JQL query
    mock_jira_client.search_issues.assert_called_once()
    call_args = mock_jira_client.search_issues.call_args[0][0]
    assert 'status = "AI Ready"' in call_args
    assert 'project IN ("PROJ")' in call_args

    # Verify handler was called
    mock_handler.handle.assert_called_once_with(issue, project_map.mappings["PROJ"])


@pytest.mark.asyncio
async def test_poll_once_skips_processed_issues(
    mock_jira_client: AsyncMock,
    project_map: ProjectMap,
    mock_handler: AsyncMock,
) -> None:
    """Test that already-processed issues are skipped on subsequent polls."""
    issue = make_jira_issue("PROJ-123")
    mock_jira_client.search_issues.return_value = [issue]

    settings = make_jira_settings()
    poller = JiraPoller(mock_jira_client, settings, project_map, mock_handler)

    # First poll — issue should be processed
    await poller._poll_once()
    assert mock_handler.handle.call_count == 1

    # Second poll — same issue should be skipped
    await poller._poll_once()
    assert mock_handler.handle.call_count == 1


@pytest.mark.asyncio
async def test_poll_once_skips_issues_not_in_project_map(
    mock_jira_client: AsyncMock,
    project_map: ProjectMap,
    mock_handler: AsyncMock,
) -> None:
    """Test that issues from projects not in the map are skipped."""
    # Issue from a different project
    issue = make_jira_issue("OTHER-456")
    mock_jira_client.search_issues.return_value = [issue]

    settings = make_jira_settings()
    poller = JiraPoller(mock_jira_client, settings, project_map, mock_handler)

    await poller._poll_once()

    # Handler should not be called for unmapped project
    mock_handler.handle.assert_not_called()


@pytest.mark.asyncio
async def test_poll_once_handles_errors_gracefully(
    mock_jira_client: AsyncMock,
    project_map: ProjectMap,
    mock_handler: AsyncMock,
) -> None:
    """Test that errors in handler are propagated (caught by _poll_loop)."""
    issue = make_jira_issue("PROJ-123")
    mock_jira_client.search_issues.return_value = [issue]
    mock_handler.handle.side_effect = Exception("Handler error")

    settings = make_jira_settings()
    poller = JiraPoller(mock_jira_client, settings, project_map, mock_handler)

    # Should propagate — _poll_loop catches it
    with pytest.raises(Exception, match="Handler error"):
        await poller._poll_once()


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle(
    mock_jira_client: AsyncMock,
    project_map: ProjectMap,
    mock_handler: AsyncMock,
) -> None:
    """Test that start() and stop() work correctly."""
    settings = make_jira_settings(poll_interval=1)
    poller = JiraPoller(mock_jira_client, settings, project_map, mock_handler)

    await poller.start()
    assert poller._task is not None
    assert not poller._task.done()

    # Let it run briefly
    await asyncio.sleep(0.05)

    await poller.stop()
    assert poller._task.done()


@pytest.mark.asyncio
async def test_poll_once_with_no_projects_in_map(
    mock_jira_client: AsyncMock,
    mock_handler: AsyncMock,
) -> None:
    """Test that _poll_once does nothing when project map is empty."""
    empty_map = ProjectMap(mappings={})
    settings = make_jira_settings()
    poller = JiraPoller(mock_jira_client, settings, empty_map, mock_handler)

    await poller._poll_once()

    # Should not call search when there are no projects
    mock_jira_client.search_issues.assert_not_called()
