"""Tests for the health check endpoint and app lifespan."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from gitlab_copilot_agent.main import lifespan
from tests.conftest import (
    JIRA_EMAIL,
    JIRA_PROJECT_MAP_JSON,
    JIRA_TOKEN,
    JIRA_URL,
)


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


@pytest.mark.asyncio
async def test_lifespan_with_jira_closes_client_on_shutdown(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that JiraClient.close() is called on app shutdown when Jira is enabled."""
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

    with patch(
        "gitlab_copilot_agent.main.JiraClient", return_value=mock_jira_client
    ), patch("gitlab_copilot_agent.main.JiraPoller", return_value=mock_poller):
        async with lifespan(test_app):
            mock_poller.start.assert_called_once()

        mock_poller.stop.assert_called_once()
        mock_jira_client.close.assert_called_once()
