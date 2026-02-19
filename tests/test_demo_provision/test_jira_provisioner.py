"""Tests for the Jira provisioner module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from demo_provision.jira_provisioner import (
    DEMO_ISSUES,
    _make_adf,
    build_client,
    create_issue,
    create_project,
    create_status,
    create_statuses,
    find_status,
    get_current_user,
    get_project,
)

from .conftest import (
    JIRA_API_TOKEN,
    JIRA_EMAIL,
    JIRA_LEAD_ACCOUNT_ID,
    JIRA_PROJECT_KEY,
    JIRA_URL,
)


class TestBuildClient:
    def test_returns_httpx_client_with_auth(self) -> None:
        client = build_client(JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN)

        assert isinstance(client, httpx.Client)
        assert "Authorization" in client.headers
        assert client.headers["Authorization"].startswith("Basic ")
        client.close()


def _mock_response(status_code: int, json_data: dict | None = None) -> httpx.Response:
    """Create an httpx.Response with a fake request attached."""
    request = httpx.Request("GET", "https://fake")
    if json_data is not None:
        return httpx.Response(status_code, json=json_data, request=request)
    return httpx.Response(status_code, request=request)


class TestGetProject:
    def test_returns_project_when_exists(self) -> None:
        client = MagicMock()
        client.get.return_value = _mock_response(200, {"key": JIRA_PROJECT_KEY, "name": "Demo"})

        result = get_project(client, JIRA_PROJECT_KEY)

        assert result is not None
        assert result["key"] == JIRA_PROJECT_KEY

    def test_returns_none_when_not_found(self) -> None:
        client = MagicMock()
        client.get.return_value = _mock_response(404)

        result = get_project(client, "NONEXISTENT")

        assert result is None


class TestCreateProject:
    def test_creates_project_with_correct_payload(self) -> None:
        client = MagicMock()
        client.post.return_value = _mock_response(201, {"id": "10001", "key": JIRA_PROJECT_KEY})

        result = create_project(
            client, JIRA_PROJECT_KEY, "Copilot Demo", lead_account_id=JIRA_LEAD_ACCOUNT_ID
        )

        client.post.assert_called_once()
        call_args = client.post.call_args
        assert call_args[0][0] == "/rest/api/3/project"
        payload = call_args[1]["json"]
        assert payload["key"] == JIRA_PROJECT_KEY
        assert payload["leadAccountId"] == JIRA_LEAD_ACCOUNT_ID
        assert result["key"] == JIRA_PROJECT_KEY


class TestCreateIssue:
    def test_creates_issue_and_returns_key(self) -> None:
        client = MagicMock()
        client.post.return_value = _mock_response(
            201, {"key": f"{JIRA_PROJECT_KEY}-1", "id": "10001"}
        )

        key = create_issue(client, JIRA_PROJECT_KEY, "Test issue", "Description text")

        assert key == f"{JIRA_PROJECT_KEY}-1"
        call_args = client.post.call_args
        payload = call_args[1]["json"]
        assert payload["fields"]["project"]["key"] == JIRA_PROJECT_KEY
        assert payload["fields"]["summary"] == "Test issue"
        assert payload["fields"]["issuetype"]["name"] == "Task"

    def test_description_uses_adf_format(self) -> None:
        client = MagicMock()
        client.post.return_value = _mock_response(
            201, {"key": f"{JIRA_PROJECT_KEY}-2", "id": "10002"}
        )

        create_issue(client, JIRA_PROJECT_KEY, "Test", "Some description")

        payload = client.post.call_args[1]["json"]
        desc = payload["fields"]["description"]
        assert desc["type"] == "doc"
        assert desc["version"] == 1
        assert len(desc["content"]) >= 1


class TestMakeAdf:
    def test_single_paragraph(self) -> None:
        result = _make_adf("Hello world")

        assert result["type"] == "doc"
        assert len(result["content"]) == 1
        assert result["content"][0]["content"][0]["text"] == "Hello world"

    def test_multiple_paragraphs(self) -> None:
        result = _make_adf("First paragraph\n\nSecond paragraph")

        assert len(result["content"]) == 2


class TestGetCurrentUser:
    def test_returns_user_data(self) -> None:
        client = MagicMock()
        client.get.return_value = _mock_response(
            200, {"accountId": JIRA_LEAD_ACCOUNT_ID, "displayName": "Test User"}
        )

        result = get_current_user(client)

        assert result["accountId"] == JIRA_LEAD_ACCOUNT_ID


class TestDemoIssues:
    def test_has_three_issues(self) -> None:
        assert len(DEMO_ISSUES) == 3

    def test_all_issues_have_required_fields(self) -> None:
        for issue in DEMO_ISSUES:
            assert "summary" in issue
            assert "description" in issue
            assert len(issue["summary"]) > 10
            assert len(issue["description"]) > 50

    def test_issues_reference_project_files(self) -> None:
        all_descriptions = " ".join(i["description"] for i in DEMO_ISSUES)
        assert "database.py" in all_descriptions
        assert "auth.py" in all_descriptions or "logging" in all_descriptions


JIRA_PROJECT_ID = "10068"
AI_READY_STATUS = "AI Ready"
IN_REVIEW_STATUS = "In Review"


class TestFindStatus:
    def test_returns_status_when_found(self) -> None:
        client = MagicMock()
        status_data = {"name": AI_READY_STATUS, "id": "10042"}
        client.get.return_value = _mock_response(200, {"values": [status_data]})

        result = find_status(client, AI_READY_STATUS)

        assert result == status_data
        client.get.assert_called_once_with(
            "/rest/api/3/statuses/search", params={"searchString": AI_READY_STATUS}
        )

    def test_returns_none_when_not_found(self) -> None:
        client = MagicMock()
        client.get.return_value = _mock_response(200, {"values": []})

        result = find_status(client, AI_READY_STATUS)

        assert result is None


class TestCreateStatus:
    def test_creates_project_scoped_status_with_correct_payload(self) -> None:
        client = MagicMock()
        created = [{"name": AI_READY_STATUS, "id": "10042"}]
        client.post.return_value = _mock_response(201, created)

        result = create_status(client, AI_READY_STATUS, JIRA_PROJECT_ID)

        assert result["name"] == AI_READY_STATUS
        call_args = client.post.call_args
        assert call_args[0][0] == "/rest/api/3/statuses"
        payload = call_args[1]["json"]
        assert payload["scope"]["type"] == "PROJECT"
        assert payload["scope"]["project"]["id"] == JIRA_PROJECT_ID
        status = payload["statuses"][0]
        assert status["name"] == AI_READY_STATUS
        assert status["statusCategory"] == "NEW"


class TestCreateStatuses:
    def test_creates_multiple_statuses_in_one_call(self) -> None:
        client = MagicMock()
        created = [
            {"name": AI_READY_STATUS, "id": "10042"},
            {"name": IN_REVIEW_STATUS, "id": "10043"},
        ]
        client.post.return_value = _mock_response(201, created)

        result = create_statuses(
            client,
            [(AI_READY_STATUS, "NEW"), (IN_REVIEW_STATUS, "INDETERMINATE")],
            JIRA_PROJECT_ID,
        )

        assert len(result) == 2
        assert result[0]["name"] == AI_READY_STATUS
        assert result[1]["name"] == IN_REVIEW_STATUS
        payload = client.post.call_args[1]["json"]
        assert payload["scope"]["type"] == "PROJECT"
        assert payload["scope"]["project"]["id"] == JIRA_PROJECT_ID
        assert len(payload["statuses"]) == 2
        assert payload["statuses"][0]["statusCategory"] == "NEW"
        assert payload["statuses"][1]["statusCategory"] == "INDETERMINATE"
