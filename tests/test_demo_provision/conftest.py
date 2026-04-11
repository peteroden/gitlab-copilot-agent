"""Shared constants and fixtures for demo provisioner tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

GITLAB_URL = "https://gitlab.example.com"
GITLAB_TOKEN = "glpat-test-token"
JIRA_URL = "https://jira.example.com"
JIRA_EMAIL = "test@example.com"
JIRA_API_TOKEN = "jira-test-token"
JIRA_PROJECT_KEY = "DEMO"
GITLAB_GROUP = "testorg"
GITLAB_PROJECT_NAME = "copilot-demo"
GITLAB_PROJECT_PATH = f"{GITLAB_GROUP}/{GITLAB_PROJECT_NAME}"
GITLAB_PROJECT_URL = f"{GITLAB_URL}/{GITLAB_PROJECT_PATH}"
JIRA_LEAD_ACCOUNT_ID = "abc123def456"

GITLAB_PROJECT_ID = 42

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "scripts" / "demo_templates" / "blog-api"

# Reusable API response payloads
PROJECT_RESPONSE: dict[str, Any] = {
    "id": GITLAB_PROJECT_ID,
    "path_with_namespace": GITLAB_PROJECT_PATH,
    "web_url": GITLAB_PROJECT_URL,
}

GROUP_RESPONSE: dict[str, Any] = {
    "id": 5,
    "full_path": GITLAB_GROUP,
}

MR_RESPONSE: dict[str, Any] = {
    "iid": 1,
    "web_url": f"{GITLAB_PROJECT_URL}/-/merge_requests/1",
}

HOOK_RESPONSE: dict[str, Any] = {"id": 10, "url": "https://example.com/webhook"}


# Type alias for the transport handler used in MockTransport
MockHandler = Callable[[httpx.Request], httpx.Response]


def make_mock_handler(
    routes: dict[tuple[str, str], httpx.Response | Callable[[httpx.Request], httpx.Response]],
    *,
    default_status: int = 404,
) -> MockHandler:
    """Build a MockTransport handler from a {(method, path_prefix): Response} map.

    Path matching uses ``startswith`` so ``/projects/`` matches
    ``/projects/testorg%2Fcopilot-demo``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v4")
        for (method, route_path), resp in routes.items():
            if request.method == method and path.startswith(route_path):
                return resp(request) if callable(resp) else resp
        return httpx.Response(default_status)

    return handler


def json_response(data: Any, status_code: int = 200) -> httpx.Response:
    """Shortcut: build an httpx.Response with JSON body."""
    return httpx.Response(status_code, json=data)


@pytest.fixture()
def gitlab_client() -> httpx.Client:
    """Provide an httpx.Client with a no-op transport (override per-test)."""
    return httpx.Client(
        base_url=f"{GITLAB_URL}/api/v4",
        headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )


@pytest.fixture()
def demo_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all required environment variables for demo provisioning."""
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("JIRA_URL", JIRA_URL)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_API_TOKEN)
