"""Tests for Jira models, client, and project mapping."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gitlab_copilot_agent.jira_client import JiraClient
from gitlab_copilot_agent.jira_models import (
    JiraIssue,
    JiraSearchResponse,
)
from gitlab_copilot_agent.project_mapping import GitLabProjectMapping, ProjectMap

# -- Constants --

JIRA_URL = "https://test.atlassian.net"
JIRA_EMAIL = "agent@test.com"
JIRA_TOKEN = "test-jira-token"

ISSUE_PAYLOAD: dict[str, Any] = {
    "id": "10001",
    "key": "PROJ-123",
    "fields": {
        "summary": "Add login feature",
        "description": "Implement OAuth login flow",
        "status": {"name": "AI Ready", "id": "100"},
        "assignee": {
            "account_id": "abc123",
            "display_name": "Jane Doe",
            "email_address": "jane@test.com",
        },
        "labels": ["backend"],
    },
}

SEARCH_RESPONSE: dict[str, Any] = {
    "issues": [ISSUE_PAYLOAD],
    "total": 1,
}

TRANSITIONS_RESPONSE: dict[str, Any] = {
    "transitions": [
        {"id": "21", "name": "In Progress"},
        {"id": "31", "name": "Done"},
    ],
}


# -- Factories --


def make_jira_client() -> JiraClient:
    """Create a JiraClient with test credentials."""
    return JiraClient(JIRA_URL, JIRA_EMAIL, JIRA_TOKEN)


# -- Model Tests --


class TestJiraModels:
    def test_project_key_extraction(self) -> None:
        issue = JiraIssue.model_validate(ISSUE_PAYLOAD)
        assert issue.project_key == "PROJ"

    def test_issue_without_assignee(self) -> None:
        payload = {**ISSUE_PAYLOAD, "fields": {**ISSUE_PAYLOAD["fields"], "assignee": None}}
        issue = JiraIssue.model_validate(payload)
        assert issue.fields.assignee is None

    def test_search_response_pagination_alias(self) -> None:
        payload = {**SEARCH_RESPONSE, "nextPageToken": "page2token"}
        resp = JiraSearchResponse.model_validate(payload)
        assert resp.next_page_token == "page2token"


# -- Client Tests --


class TestJiraClientSearch:
    async def test_search_issues_single_page(self) -> None:
        client = make_jira_client()
        mock_resp = AsyncMock(spec=httpx.Response)
        mock_resp.json.return_value = SEARCH_RESPONSE
        mock_resp.raise_for_status = lambda: None

        with patch.object(client._client, "get", return_value=mock_resp) as mock_get:
            issues = await client.search_issues('status = "AI Ready"')

        assert len(issues) == 1
        assert issues[0].key == "PROJ-123"
        mock_get.assert_called_once_with(
            "/rest/api/3/search/jql",
            params={"jql": 'status = "AI Ready"', "maxResults": "50"},
        )
        await client.close()

    async def test_search_issues_paginated(self) -> None:
        client = make_jira_client()

        page1_resp = AsyncMock(spec=httpx.Response)
        page1_resp.json.return_value = {**SEARCH_RESPONSE, "nextPageToken": "tok2"}
        page1_resp.raise_for_status = lambda: None

        page2_resp = AsyncMock(spec=httpx.Response)
        page2_resp.json.return_value = SEARCH_RESPONSE
        page2_resp.raise_for_status = lambda: None

        with patch.object(
            client._client, "get", side_effect=[page1_resp, page2_resp]
        ):
            issues = await client.search_issues("project = PROJ")

        assert len(issues) == 2
        await client.close()


class TestJiraClientTransition:
    async def test_transition_issue(self) -> None:
        client = make_jira_client()

        get_resp = AsyncMock(spec=httpx.Response)
        get_resp.json.return_value = TRANSITIONS_RESPONSE
        get_resp.raise_for_status = lambda: None

        post_resp = AsyncMock(spec=httpx.Response)
        post_resp.raise_for_status = lambda: None

        with patch.object(client._client, "get", return_value=get_resp), \
             patch.object(client._client, "post", return_value=post_resp) as mock_post:
            await client.transition_issue("PROJ-123", "In Progress")

        mock_post.assert_called_once_with(
            "/rest/api/3/issue/PROJ-123/transitions",
            json={"transition": {"id": "21"}},
        )
        await client.close()

    async def test_transition_case_insensitive(self) -> None:
        client = make_jira_client()

        get_resp = AsyncMock(spec=httpx.Response)
        get_resp.json.return_value = TRANSITIONS_RESPONSE
        get_resp.raise_for_status = lambda: None

        post_resp = AsyncMock(spec=httpx.Response)
        post_resp.raise_for_status = lambda: None

        with patch.object(client._client, "get", return_value=get_resp), \
             patch.object(client._client, "post", return_value=post_resp):
            await client.transition_issue("PROJ-123", "in progress")
        await client.close()

    async def test_transition_unavailable_raises(self) -> None:
        client = make_jira_client()

        get_resp = AsyncMock(spec=httpx.Response)
        get_resp.json.return_value = TRANSITIONS_RESPONSE
        get_resp.raise_for_status = lambda: None

        with patch.object(client._client, "get", return_value=get_resp):
            with pytest.raises(ValueError, match="No transition to 'Blocked'"):
                await client.transition_issue("PROJ-123", "Blocked")
        await client.close()


class TestJiraClientComment:
    async def test_add_comment(self) -> None:
        client = make_jira_client()

        post_resp = AsyncMock(spec=httpx.Response)
        post_resp.raise_for_status = lambda: None

        with patch.object(client._client, "post", return_value=post_resp) as mock_post:
            await client.add_comment("PROJ-123", "MR created: https://gitlab.com/mr/1")

        call_args = mock_post.call_args
        assert call_args[0][0] == "/rest/api/3/issue/PROJ-123/comment"
        body = call_args[1]["json"]["body"]
        assert body["type"] == "doc"
        assert body["content"][0]["content"][0]["text"] == "MR created: https://gitlab.com/mr/1"
        await client.close()


# -- Project Mapping Tests --


class TestProjectMapping:
    def test_lookup_existing_project(self) -> None:
        pm = ProjectMap(mappings={
            "PROJ": GitLabProjectMapping(
                gitlab_project_id=12345,
                clone_url="https://gitlab.com/group/repo.git",
            ),
        })
        result = pm.get("PROJ")
        assert result is not None
        assert result.gitlab_project_id == 12345
        assert result.target_branch == "main"

    def test_lookup_missing_project(self) -> None:
        pm = ProjectMap(mappings={})
        assert pm.get("UNKNOWN") is None

    def test_contains(self) -> None:
        pm = ProjectMap(mappings={
            "PROJ": GitLabProjectMapping(
                gitlab_project_id=1, clone_url="https://example.com/repo.git"
            ),
        })
        assert "PROJ" in pm
        assert "OTHER" not in pm

    def test_custom_target_branch(self) -> None:
        mapping = GitLabProjectMapping(
            gitlab_project_id=1,
            clone_url="https://example.com/repo.git",
            target_branch="develop",
        )
        assert mapping.target_branch == "develop"
