from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from gitlab_copilot_agent.jira_poller import JiraPoller
from gitlab_copilot_agent.main import app, lifespan
from gitlab_copilot_agent.project_registry import ProjectRegistry, ResolvedProject
from tests.conftest import (
    EXAMPLE_CLONE_URL,
    GITLAB_TOKEN,
    JIRA_EMAIL,
    JIRA_TOKEN,
    JIRA_URL,
    PROJECT_ID,
    WEBHOOK_SECRET,
)

SECOND_TOKEN = "glpat-second"  # noqa: S105


def _settings() -> dict[str, str | int]:
    return {
        "url": JIRA_URL,
        "email": JIRA_EMAIL,
        "api_token": JIRA_TOKEN,
        "trigger_status": "AI Ready",
        "in_progress_status": "In Progress",
        "poll_interval": 1,
        "project_map_json": '{"mappings": {}}',
    }


def _proj(
    jira: str = "PROJ",
    pid: int = PROJECT_ID,
    token: str = GITLAB_TOKEN,
) -> ResolvedProject:
    return ResolvedProject(
        jira_project=jira,
        repo="group/project",
        gitlab_project_id=pid,
        clone_url=EXAMPLE_CLONE_URL,
        target_branch="main",
        credential_ref="default",
        token=token,
    )


def _registry(*projs: ResolvedProject) -> ProjectRegistry:
    return ProjectRegistry(list(projs) if projs else [_proj()])


async def test_reload_registry_swaps_map() -> None:
    from gitlab_copilot_agent.config import JiraSettings

    settings = JiraSettings(**_settings())  # type: ignore[arg-type]
    poller = JiraPoller(AsyncMock(), settings, _registry(), AsyncMock())

    new_reg = _registry(_proj(), _proj(jira="OPS", pid=99))
    await poller.reload_registry(new_reg)

    assert poller._project_map is new_reg
    assert poller._project_map.jira_keys() == {"PROJ", "OPS"}


async def test_reload_registry_clears_processed() -> None:
    from gitlab_copilot_agent.config import JiraSettings

    settings = JiraSettings(**_settings())  # type: ignore[arg-type]
    poller = JiraPoller(AsyncMock(), settings, _registry(), AsyncMock())
    poller._processed_issues.add("PROJ-1")

    await poller.reload_registry(_registry())

    assert len(poller._processed_issues) == 0


async def test_reload_endpoint_returns_keys(
    env_vars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_URL", JIRA_URL)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
    monkeypatch.setenv("JIRA_TRIGGER_STATUS", "AI Ready")
    monkeypatch.setenv("JIRA_IN_PROGRESS_STATUS", "In Progress")
    monkeypatch.setenv(
        "JIRA_PROJECT_MAP",
        '{"mappings": {"PROJ": {"repo": "group/project", '
        '"target_branch": "main", "credential_ref": "default"}}}',
    )

    mock_poller = AsyncMock()
    mock_poller.start = AsyncMock()
    mock_poller.stop = AsyncMock()
    mock_poller.reload_registry = AsyncMock()

    with (
        patch("gitlab_copilot_agent.main.JiraClient"),
        patch("gitlab_copilot_agent.main.JiraPoller", return_value=mock_poller),
        patch("gitlab_copilot_agent.main.CodingOrchestrator"),
        patch("gitlab_copilot_agent.main.CredentialRegistry"),
        patch("gitlab_copilot_agent.main.ProjectRegistry") as mock_reg_cls,
    ):
        mock_reg_cls.from_rendered_map = AsyncMock(return_value=_registry())

        async with lifespan(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/config/reload",
                    json={
                        "mappings": {
                            "PROJ": {
                                "repo": "group/project",
                                "target_branch": "main",
                                "credential_ref": "default",
                            },
                        }
                    },
                    headers={"X-Gitlab-Token": WEBHOOK_SECRET},
                )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "PROJ" in data["jira_keys"]
    mock_poller.reload_registry.assert_awaited_once()


async def test_reload_endpoint_rejects_unauthenticated(
    env_vars: None,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/config/reload",
            json={"mappings": {}},
        )
    assert resp.status_code == 401
