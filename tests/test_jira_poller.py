"""Tests for Jira poller."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gitlab_copilot_agent.config import JiraSettings
from gitlab_copilot_agent.jira_models import JiraIssue, JiraIssueFields, JiraStatus
from gitlab_copilot_agent.jira_poller import JiraPoller
from gitlab_copilot_agent.project_registry import ProjectRegistry, ResolvedProject
from gitlab_copilot_agent.task_executor import TaskExecutionError
from tests.conftest import (
    EXAMPLE_CLONE_URL,
    GITLAB_TOKEN,
    JIRA_EMAIL,
    JIRA_TOKEN,
    JIRA_URL,
    PROJECT_ID,
)


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
def project_map() -> ProjectRegistry:
    """Project registry with a single test mapping."""
    return ProjectRegistry(
        [
            ResolvedProject(
                jira_project="PROJ",
                repo="group/project",
                gitlab_project_id=PROJECT_ID,
                clone_url=EXAMPLE_CLONE_URL,
                target_branch="main",
                credential_ref="default",
                token=GITLAB_TOKEN,
            )
        ]
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
    project_map: ProjectRegistry,
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

    # Verify handler was called with the resolved project
    mock_handler.handle.assert_called_once()
    call_args = mock_handler.handle.call_args[0]
    assert call_args[0] == issue
    assert call_args[1].gitlab_project_id == PROJECT_ID


@pytest.mark.asyncio
async def test_poll_once_skips_processed_issues(
    mock_jira_client: AsyncMock,
    project_map: ProjectRegistry,
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
    project_map: ProjectRegistry,
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
    project_map: ProjectRegistry,
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
async def test_poll_once_task_execution_error_is_not_marked_processed(
    mock_jira_client: AsyncMock,
    project_map: ProjectRegistry,
    mock_handler: AsyncMock,
) -> None:
    issue = make_jira_issue("PROJ-123")
    mock_jira_client.search_issues.return_value = [issue]
    mock_handler.handle.side_effect = TaskExecutionError("runner error")

    settings = make_jira_settings()
    poller = JiraPoller(mock_jira_client, settings, project_map, mock_handler)

    await poller._poll_once()
    await poller._poll_once()

    assert mock_handler.handle.call_count == 2


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle(
    mock_jira_client: AsyncMock,
    project_map: ProjectRegistry,
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
    empty_registry = ProjectRegistry([])
    settings = make_jira_settings()
    poller = JiraPoller(mock_jira_client, settings, empty_registry, mock_handler)

    await poller._poll_once()

    # Should not call search when there are no projects
    mock_jira_client.search_issues.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed_project_ids", "expect_handled"),
    [
        pytest.param({PROJECT_ID}, True, id="project-in-allowlist"),
        pytest.param({99999}, False, id="project-not-in-allowlist"),
        pytest.param(None, True, id="no-allowlist"),
    ],
)
async def test_poll_once_allowlist_enforcement(
    mock_jira_client: AsyncMock,
    project_map: ProjectRegistry,
    mock_handler: AsyncMock,
    allowed_project_ids: set[int] | None,
    expect_handled: bool,
) -> None:
    issue = make_jira_issue("PROJ-123")
    mock_jira_client.search_issues.return_value = [issue]

    settings = make_jira_settings()
    poller = JiraPoller(
        mock_jira_client,
        settings,
        project_map,
        mock_handler,
        allowed_project_ids=allowed_project_ids,
    )

    await poller._poll_once()

    if expect_handled:
        mock_handler.handle.assert_called_once()
    else:
        mock_handler.handle.assert_not_called()


# ---------------------------------------------------------------------------
# Grouped JQL tests
# ---------------------------------------------------------------------------

OPS_PROJECT_ID = 999
OPS_CLONE_URL = "https://gitlab.example.com/group/platform-tools.git"
CUSTOM_TRIGGER_STATUS = "Ready for Dev"


def _two_project_registry(
    proj_trigger: str = "AI Ready",
    ops_trigger: str = CUSTOM_TRIGGER_STATUS,
) -> ProjectRegistry:
    return ProjectRegistry(
        [
            ResolvedProject(
                jira_project="PROJ",
                repo="group/project",
                gitlab_project_id=PROJECT_ID,
                clone_url=EXAMPLE_CLONE_URL,
                target_branch="main",
                credential_ref="default",
                token=GITLAB_TOKEN,
                trigger_status=proj_trigger,
            ),
            ResolvedProject(
                jira_project="OPS",
                repo="group/platform-tools",
                gitlab_project_id=OPS_PROJECT_ID,
                clone_url=OPS_CLONE_URL,
                target_branch="develop",
                credential_ref="default",
                token=GITLAB_TOKEN,
                trigger_status=ops_trigger,
            ),
        ]
    )


class TestGroupedJQL:
    @pytest.mark.asyncio
    async def test_grouped_jql_two_statuses(
        self, mock_jira_client: AsyncMock, mock_handler: AsyncMock
    ) -> None:
        """Two projects with different trigger statuses produce two JQL queries."""
        mock_jira_client.search_issues.return_value = []
        registry = _two_project_registry()
        settings = make_jira_settings()
        poller = JiraPoller(mock_jira_client, settings, registry, mock_handler)

        await poller._poll_once()

        assert mock_jira_client.search_issues.call_count == 2
        all_jqls = [call[0][0] for call in mock_jira_client.search_issues.call_args_list]
        assert any('status = "AI Ready"' in jql for jql in all_jqls)
        assert any(f'status = "{CUSTOM_TRIGGER_STATUS}"' in jql for jql in all_jqls)

    @pytest.mark.asyncio
    async def test_grouped_jql_same_status(
        self, mock_jira_client: AsyncMock, mock_handler: AsyncMock
    ) -> None:
        """Two projects sharing the same trigger status produce a single JQL query."""
        mock_jira_client.search_issues.return_value = []
        registry = _two_project_registry(proj_trigger="AI Ready", ops_trigger="AI Ready")
        settings = make_jira_settings()
        poller = JiraPoller(mock_jira_client, settings, registry, mock_handler)

        await poller._poll_once()

        assert mock_jira_client.search_issues.call_count == 1
        jql = mock_jira_client.search_issues.call_args[0][0]
        assert 'status = "AI Ready"' in jql
        assert "PROJ" in jql
        assert "OPS" in jql

    @pytest.mark.asyncio
    async def test_grouped_jql_single_project_default_status(
        self, mock_jira_client: AsyncMock, project_map: ProjectRegistry, mock_handler: AsyncMock
    ) -> None:
        """Single project with default status produces JQL with default status."""
        mock_jira_client.search_issues.return_value = []
        settings = make_jira_settings()
        poller = JiraPoller(mock_jira_client, settings, project_map, mock_handler)

        await poller._poll_once()

        assert mock_jira_client.search_issues.call_count == 1
        jql = mock_jira_client.search_issues.call_args[0][0]
        assert 'status = "AI Ready"' in jql
        assert 'project IN ("PROJ")' in jql
