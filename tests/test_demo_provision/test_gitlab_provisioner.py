"""Tests for the GitLab provisioner module."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from demo_provision.gitlab_provisioner import (
    create_project,
    create_webhook,
    get_namespace,
    get_project,
    load_template,
    push_files,
)

from .conftest import (
    GITLAB_GROUP,
    GITLAB_PROJECT_ID,
    GITLAB_PROJECT_NAME,
    GITLAB_PROJECT_PATH,
    GITLAB_TOKEN,
    GITLAB_URL,
    GROUP_RESPONSE,
    HOOK_RESPONSE,
    PROJECT_RESPONSE,
    TEMPLATE_DIR,
    json_response,
)


def _make_client(handler: httpx.MockTransport | None = None) -> httpx.Client:
    """Build a test client with a mock transport."""
    transport = handler or httpx.MockTransport(lambda _: httpx.Response(500))
    return httpx.Client(
        base_url=f"{GITLAB_URL}/api/v4",
        headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
        transport=transport,
    )


class TestGetProject:
    def test_returns_project_when_exists(self) -> None:
        transport = httpx.MockTransport(lambda _: json_response(PROJECT_RESPONSE))
        client = _make_client(transport)

        result = get_project(client, GITLAB_PROJECT_PATH)

        assert result is not None
        assert result["id"] == GITLAB_PROJECT_ID

    def test_returns_none_when_not_found(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(404))
        client = _make_client(transport)

        result = get_project(client, GITLAB_PROJECT_PATH)

        assert result is None


class TestCreateProject:
    def test_creates_project_with_correct_params(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            body = request.read()
            import json

            data = json.loads(body)
            assert data["name"] == GITLAB_PROJECT_NAME
            assert data["namespace_id"] == 10
            assert data["visibility"] == "private"
            assert data["initialize_with_readme"] is True
            return json_response({**PROJECT_RESPONSE, "id": 99})

        client = _make_client(httpx.MockTransport(handler))
        result = create_project(client, GITLAB_PROJECT_NAME, 10)

        assert result["id"] == 99

    def test_creates_project_with_custom_visibility(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            data = json.loads(request.read())
            assert data["visibility"] == "public"
            return json_response(PROJECT_RESPONSE)

        client = _make_client(httpx.MockTransport(handler))
        create_project(client, GITLAB_PROJECT_NAME, 10, visibility="public")


class TestGetNamespace:
    def test_returns_group(self) -> None:
        transport = httpx.MockTransport(lambda _: json_response(GROUP_RESPONSE))
        client = _make_client(transport)

        result = get_namespace(client, GITLAB_GROUP)

        assert result["id"] == 5

    def test_falls_back_to_namespace_search(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.removeprefix("/api/v4")
            if path.startswith("/groups/"):
                return httpx.Response(404)
            if path == "/namespaces":
                return json_response([{"full_path": GITLAB_GROUP, "id": 7}])
            return httpx.Response(500)

        client = _make_client(httpx.MockTransport(handler))
        result = get_namespace(client, GITLAB_GROUP)

        assert result["id"] == 7

    def test_exits_when_group_not_found(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.removeprefix("/api/v4")
            if path.startswith("/groups/"):
                return httpx.Response(404)
            if path == "/namespaces":
                return json_response([])
            return httpx.Response(500)

        client = _make_client(httpx.MockTransport(handler))

        with pytest.raises(SystemExit):
            get_namespace(client, "nonexistent-group")


class TestPushFiles:
    def test_pushes_files_as_create_actions(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.removeprefix("/api/v4")
            if "/repository/tree" in path:
                return json_response([])
            if "/repository/commits" in path:
                calls.append(request)
                return json_response({"id": "abc123"})
            return httpx.Response(500)

        client = _make_client(httpx.MockTransport(handler))
        files = {"main.py": "print('hello')", "README.md": "# Demo"}

        push_files(client, GITLAB_PROJECT_ID, "main", files, "Initial commit")

        assert len(calls) == 1
        import json

        commit_data = json.loads(calls[0].read())
        assert commit_data["branch"] == "main"
        assert commit_data["commit_message"] == "Initial commit"
        assert len(commit_data["actions"]) == 2
        assert all(a["action"] == "create" for a in commit_data["actions"])


class TestCreateWebhook:
    def test_creates_webhook_with_correct_params(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return json_response(HOOK_RESPONSE)

        client = _make_client(httpx.MockTransport(handler))
        create_webhook(client, GITLAB_PROJECT_ID, "https://example.com/webhook", "secret123")

        assert len(calls) == 1
        import json

        data = json.loads(calls[0].read())
        assert data == {
            "url": "https://example.com/webhook",
            "token": "secret123",
            "merge_requests_events": True,
            "note_events": True,
            "push_events": False,
            "enable_ssl_verification": True,
        }


class TestLoadTemplate:
    def test_loads_all_template_files(self) -> None:
        files = load_template(TEMPLATE_DIR)

        assert len(files) >= 10
        assert "src/demo_app/main.py" in files
        assert "src/demo_app/database.py" in files
        assert "src/demo_app/auth.py" in files
        assert "AGENTS.md" in files
        assert ".github/skills/security-patterns/SKILL.md" in files
        assert ".github/agents/security-reviewer.agent.md" in files

    def test_template_files_are_non_empty(self) -> None:
        files = load_template(TEMPLATE_DIR)

        for path, content in files.items():
            if path.endswith("__init__.py"):
                continue
            assert content.strip(), f"{path} is empty"
