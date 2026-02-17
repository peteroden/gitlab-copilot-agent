"""Tests for the health check endpoint and app lifespan."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from gitlab_copilot_agent.concurrency import RepoLockManager
from gitlab_copilot_agent.main import _create_executor, lifespan
from gitlab_copilot_agent.task_executor import LocalTaskExecutor
from tests.conftest import (
    JIRA_EMAIL,
    JIRA_PROJECT_MAP_JSON,
    JIRA_TOKEN,
    JIRA_URL,
)


def test_create_executor_local() -> None:
    assert isinstance(_create_executor("local"), LocalTaskExecutor)


def test_create_executor_k8s_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="kubernetes"):
        _create_executor("kubernetes")


@pytest.mark.usefixtures("env_vars")
async def test_health_returns_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_lifespan_without_jira_starts_and_stops(env_vars: None) -> None:
    """Test that lifespan completes successfully when Jira is not enabled."""
    test_app = FastAPI()
    async with lifespan(test_app):
        assert test_app.state.settings is not None
        assert test_app.state.settings.jira is None
        assert test_app.state.repo_locks is not None
        assert isinstance(test_app.state.repo_locks, RepoLockManager)
        assert isinstance(test_app.state.executor, LocalTaskExecutor)


@pytest.mark.asyncio
async def test_lifespan_with_jira_creates_shared_lock_manager(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that shared RepoLockManager is created and passed to orchestrator."""
    monkeypatch.setenv("JIRA_URL", JIRA_URL)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
    monkeypatch.setenv("JIRA_TRIGGER_STATUS", "AI Ready")
    monkeypatch.setenv("JIRA_IN_PROGRESS_STATUS", "In Progress")
    monkeypatch.setenv("JIRA_PROJECT_MAP", JIRA_PROJECT_MAP_JSON)

    test_app = FastAPI()

    mock_jira_client = AsyncMock()
    mock_jira_client.close = AsyncMock()

    mock_poller = AsyncMock()
    mock_poller.start = AsyncMock()
    mock_poller.stop = AsyncMock()

    mock_orchestrator = AsyncMock()

    with (
        patch("gitlab_copilot_agent.main.JiraClient", return_value=mock_jira_client),
        patch("gitlab_copilot_agent.main.JiraPoller", return_value=mock_poller),
        patch(
            "gitlab_copilot_agent.main.CodingOrchestrator", return_value=mock_orchestrator
        ) as mock_orch_class,
    ):
        async with lifespan(test_app):
            mock_poller.start.assert_called_once()
            assert test_app.state.repo_locks is not None
            assert isinstance(test_app.state.repo_locks, RepoLockManager)
            mock_orch_class.assert_called_once()
            args, _kwargs = mock_orch_class.call_args
            assert isinstance(args[3], LocalTaskExecutor)
            assert args[4] is test_app.state.repo_locks

        mock_poller.stop.assert_called_once()
