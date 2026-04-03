"""Tests for the health check endpoint and app lifespan."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from gitlab_copilot_agent.concurrency import DeduplicationStore, RepoLockManager
from gitlab_copilot_agent.main import _create_executor, lifespan
from gitlab_copilot_agent.task_executor import LocalTaskExecutor, TaskExecutor
from tests.conftest import (
    JIRA_EMAIL,
    JIRA_PROJECT_MAP_JSON,
    JIRA_TOKEN,
    JIRA_URL,
    make_settings,
)


def test_create_executor_local() -> None:
    assert isinstance(_create_executor("local"), LocalTaskExecutor)


def test_create_executor_k8s_requires_settings() -> None:
    with pytest.raises(ValueError, match="Settings required"):
        _create_executor("kubernetes")


def test_create_executor_k8s_returns_executor() -> None:
    settings = make_settings(
        task_executor="kubernetes",
    )
    with (
        patch("gitlab_copilot_agent.main.create_result_store"),
        patch("gitlab_copilot_agent.main.create_task_queue"),
    ):
        executor = _create_executor("kubernetes", settings)
    assert isinstance(executor, TaskExecutor)


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
        assert isinstance(test_app.state.dedup_store, DeduplicationStore)
        assert isinstance(test_app.state.executor, LocalTaskExecutor)
        assert test_app.state.project_registry is None


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
        patch("gitlab_copilot_agent.main.CredentialRegistry"),
        patch("gitlab_copilot_agent.main.ProjectRegistry") as mock_registry_cls,
    ):
        mock_registry = AsyncMock()
        mock_registry_cls.from_rendered_map = AsyncMock(return_value=mock_registry)
        async with lifespan(test_app):
            mock_poller.start.assert_called_once()
            assert test_app.state.repo_locks is not None
            assert isinstance(test_app.state.repo_locks, RepoLockManager)
            assert test_app.state.project_registry is mock_registry
            mock_orch_class.assert_called_once()
            args, _kwargs = mock_orch_class.call_args
            assert isinstance(args[3], LocalTaskExecutor)
            assert args[4] is test_app.state.repo_locks

        mock_poller.stop.assert_called_once()


EXPECTED_SHUTDOWN_ORDER = [
    "jira_poller_stop",
    "jira_client_close",
    "dedup_store_close",
    "repo_locks_close",
    "telemetry_flush",
]


@pytest.mark.asyncio
async def test_shutdown_call_ordering(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify shutdown calls happen in the correct order with structured logging."""
    call_order: list[str] = []

    mock_poller = AsyncMock()
    mock_poller.start = AsyncMock()
    mock_poller.stop = AsyncMock(side_effect=lambda: call_order.append("jira_poller_stop"))

    mock_jira = AsyncMock()
    mock_jira.close = AsyncMock(side_effect=lambda: call_order.append("jira_client_close"))

    mock_dedup = AsyncMock()
    mock_dedup.aclose = AsyncMock(side_effect=lambda: call_order.append("dedup_store_close"))

    mock_locks = AsyncMock()
    mock_locks.aclose = AsyncMock(side_effect=lambda: call_order.append("repo_locks_close"))

    monkeypatch.setenv("JIRA_URL", JIRA_URL)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
    monkeypatch.setenv("JIRA_TRIGGER_STATUS", "AI Ready")
    monkeypatch.setenv("JIRA_IN_PROGRESS_STATUS", "In Progress")
    monkeypatch.setenv("JIRA_PROJECT_MAP", JIRA_PROJECT_MAP_JSON)

    test_app = FastAPI()

    with (
        patch("gitlab_copilot_agent.main.JiraClient", return_value=mock_jira),
        patch("gitlab_copilot_agent.main.JiraPoller", return_value=mock_poller),
        patch("gitlab_copilot_agent.main.CodingOrchestrator"),
        patch("gitlab_copilot_agent.main.CredentialRegistry"),
        patch("gitlab_copilot_agent.main.ProjectRegistry") as mock_reg,
        patch("gitlab_copilot_agent.main.create_lock", return_value=mock_locks),
        patch("gitlab_copilot_agent.main.create_dedup", return_value=mock_dedup),
        patch(
            "gitlab_copilot_agent.main.shutdown_telemetry",
            side_effect=lambda: call_order.append("telemetry_flush"),
        ),
    ):
        mock_reg.from_rendered_map = AsyncMock(return_value=AsyncMock())
        async with lifespan(test_app):
            pass

    assert call_order == EXPECTED_SHUTDOWN_ORDER


@pytest.mark.asyncio
async def test_shutdown_continues_when_step_fails(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify remaining steps run even if one step raises."""
    call_order: list[str] = []

    mock_dedup = AsyncMock()
    mock_dedup.aclose = AsyncMock(side_effect=RuntimeError("connection lost"))

    mock_locks = AsyncMock()
    mock_locks.aclose = AsyncMock(side_effect=lambda: call_order.append("repo_locks_close"))

    test_app = FastAPI()

    with (
        patch("gitlab_copilot_agent.main.create_lock", return_value=mock_locks),
        patch("gitlab_copilot_agent.main.create_dedup", return_value=mock_dedup),
        patch(
            "gitlab_copilot_agent.main.shutdown_telemetry",
            side_effect=lambda: call_order.append("telemetry_flush"),
        ),
    ):
        async with lifespan(test_app):
            pass

    assert "repo_locks_close" in call_order
    assert "telemetry_flush" in call_order


@pytest.mark.asyncio
async def test_shutdown_continues_when_step_times_out(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify remaining steps run even if one step hangs past the timeout."""
    import asyncio

    call_order: list[str] = []

    monkeypatch.setenv("SHUTDOWN_TIMEOUT", "2")

    async def _hang_forever() -> None:
        await asyncio.sleep(999)

    mock_dedup = AsyncMock()
    mock_dedup.aclose = _hang_forever

    mock_locks = AsyncMock()
    mock_locks.aclose = AsyncMock(side_effect=lambda: call_order.append("repo_locks_close"))

    test_app = FastAPI()

    with (
        patch("gitlab_copilot_agent.main.create_lock", return_value=mock_locks),
        patch("gitlab_copilot_agent.main.create_dedup", return_value=mock_dedup),
        patch(
            "gitlab_copilot_agent.main.shutdown_telemetry",
            side_effect=lambda: call_order.append("telemetry_flush"),
        ),
    ):
        async with lifespan(test_app):
            pass

    assert "repo_locks_close" in call_order
    assert "telemetry_flush" in call_order


# -- Allowlist tests --

RESOLVED_PROJECT_ID = 42


@pytest.mark.asyncio
async def test_lifespan_resolves_allowlist(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When GITLAB_PROJECTS is set, lifespan resolves project IDs."""
    monkeypatch.setenv("GITLAB_PROJECTS", "group/project, 99")
    test_app = FastAPI()

    mock_client = AsyncMock()
    mock_client.resolve_project = AsyncMock(side_effect=[RESOLVED_PROJECT_ID, 99])
    with patch("gitlab_copilot_agent.main.GitLabClient", return_value=mock_client):
        async with lifespan(test_app):
            assert test_app.state.allowed_project_ids == {RESOLVED_PROJECT_ID, 99}


@pytest.mark.asyncio
async def test_lifespan_allowlist_none_when_unset(env_vars: None) -> None:
    """When GITLAB_PROJECTS is not set, allowed_project_ids is None."""
    test_app = FastAPI()
    async with lifespan(test_app):
        assert test_app.state.allowed_project_ids is None


# -- GitLab poller wiring tests --

POLLER_PROJECT_ID = 42


@pytest.mark.asyncio
async def test_lifespan_starts_gitlab_poller(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When GITLAB_POLL=true and GITLAB_PROJECTS set, poller starts."""
    monkeypatch.setenv("GITLAB_POLL", "true")
    monkeypatch.setenv("GITLAB_PROJECTS", "group/project")
    test_app = FastAPI()

    mock_gl = AsyncMock()
    mock_gl.resolve_project = AsyncMock(return_value=POLLER_PROJECT_ID)

    mock_poller = AsyncMock()
    mock_poller.start = AsyncMock()
    mock_poller.stop = AsyncMock()
    mock_poller._interval = 30

    with (
        patch("gitlab_copilot_agent.main.GitLabClient", return_value=mock_gl),
        patch(
            "gitlab_copilot_agent.main.GitLabPoller", return_value=mock_poller
        ) as mock_poller_cls,
    ):
        async with lifespan(test_app):
            mock_poller.start.assert_called_once()
            assert test_app.state.gl_poller is mock_poller
            # Verify project_registry=None passed (no Jira configured)
            _, kwargs = mock_poller_cls.call_args
            assert kwargs.get("project_registry") is None
        mock_poller.stop.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_no_poller_when_poll_disabled(env_vars: None) -> None:
    """When GITLAB_POLL is not set, no poller is created."""
    test_app = FastAPI()
    async with lifespan(test_app):
        assert not hasattr(test_app.state, "gl_poller")


def test_config_poll_requires_projects(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GITLAB_POLL=true without GITLAB_PROJECTS raises."""
    monkeypatch.setenv("GITLAB_POLL", "true")
    with pytest.raises(ValueError, match="GITLAB_PROJECTS is required"):
        make_settings(gitlab_poll=True)


def test_config_poll_rejects_empty_projects(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GITLAB_POLL=true with whitespace-only GITLAB_PROJECTS raises."""
    monkeypatch.setenv("GITLAB_POLL", "true")
    monkeypatch.setenv("GITLAB_PROJECTS", "  , , ")
    with pytest.raises(ValueError, match="GITLAB_PROJECTS is required"):
        make_settings(gitlab_poll=True, gitlab_projects="  , , ")


@pytest.mark.usefixtures("env_vars")
async def test_health_includes_poller_status(client: AsyncClient) -> None:
    """Health endpoint includes gitlab_poller when active."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    # Without poller, no gitlab_poller key
    assert "gitlab_poller" not in resp.json()


@pytest.mark.usefixtures("env_vars")
async def test_health_with_poller(client: AsyncClient) -> None:
    """Health endpoint includes poller status when poller is running."""
    from unittest.mock import MagicMock

    from gitlab_copilot_agent.main import app

    mock_task = MagicMock()
    mock_task.done.return_value = False

    mock_poller = MagicMock()
    mock_poller._task = mock_task
    mock_poller._failures = 0
    mock_poller._watermark = "2026-01-01T00:00:00Z"
    app.state.gl_poller = mock_poller

    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["gitlab_poller"]["running"] is True
    assert data["gitlab_poller"]["failures"] == 0
    assert data["gitlab_poller"]["watermark"] == "2026-01-01T00:00:00Z"

    del app.state.gl_poller


@pytest.mark.asyncio
async def test_jira_status_env_vars_backfill_into_rendered_bindings(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global JIRA_*_STATUS env vars backfill into RenderedBindings missing explicit overrides."""
    monkeypatch.setenv("JIRA_URL", JIRA_URL)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
    monkeypatch.setenv("JIRA_TRIGGER_STATUS", "Selected for Development")
    monkeypatch.setenv("JIRA_IN_PROGRESS_STATUS", "Working")
    monkeypatch.setenv("JIRA_IN_REVIEW_STATUS", "Code Review")
    monkeypatch.setenv("JIRA_PROJECT_MAP", JIRA_PROJECT_MAP_JSON)

    test_app = FastAPI()
    captured_rendered = {}

    async def capture_rendered(rendered, *_a, **_kw):
        for key, binding in rendered.mappings.items():
            captured_rendered[key] = {
                "trigger_status": binding.trigger_status,
                "in_progress_status": binding.in_progress_status,
                "in_review_status": binding.in_review_status,
            }
        return AsyncMock()

    with (
        patch("gitlab_copilot_agent.main.JiraClient", return_value=AsyncMock(close=AsyncMock())),
        patch(
            "gitlab_copilot_agent.main.JiraPoller",
            return_value=AsyncMock(start=AsyncMock(), stop=AsyncMock()),
        ),
        patch("gitlab_copilot_agent.main.CodingOrchestrator"),
        patch("gitlab_copilot_agent.main.CredentialRegistry"),
        patch("gitlab_copilot_agent.main.ProjectRegistry") as mock_reg_cls,
    ):
        mock_reg_cls.from_rendered_map = AsyncMock(side_effect=capture_rendered)
        async with lifespan(test_app):
            pass

    assert captured_rendered["PROJ"]["trigger_status"] == "Selected for Development"
    assert captured_rendered["PROJ"]["in_progress_status"] == "Working"
    assert captured_rendered["PROJ"]["in_review_status"] == "Code Review"
