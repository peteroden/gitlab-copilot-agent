"""Jira REST API client for issue search, transitions, and comments."""

from __future__ import annotations

import base64
from typing import Protocol

import httpx
import structlog

from gitlab_copilot_agent.jira_models import (
    JiraIssue,
    JiraSearchResponse,
    JiraTransitionsResponse,
)

log = structlog.get_logger()


class JiraClientProtocol(Protocol):
    """Interface for Jira API operations."""

    async def search_issues(self, jql: str) -> list[JiraIssue]: ...
    async def transition_issue(self, issue_key: str, target_status: str) -> None: ...
    async def add_comment(self, issue_key: str, body: str) -> None: ...


class JiraClient:
    """Jira REST API v3 client using basic auth (email + API token or PAT)."""

    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        auth_bytes = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Basic {auth_bytes}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def search_issues(self, jql: str) -> list[JiraIssue]:
        """Search for issues using JQL via the v3 search endpoint."""
        all_issues: list[JiraIssue] = []
        next_page_token: str | None = None

        while True:
            params: dict[str, str] = {"jql": jql, "maxResults": "50"}
            if next_page_token:
                params["nextPageToken"] = next_page_token

            resp = await self._client.get("/rest/api/3/search/jql", params=params)
            resp.raise_for_status()

            search_resp = JiraSearchResponse.model_validate(resp.json())
            all_issues.extend(search_resp.issues)

            if not search_resp.next_page_token:
                break
            next_page_token = search_resp.next_page_token

        await log.ainfo("jira_search_complete", jql=jql, count=len(all_issues))
        return all_issues

    async def transition_issue(self, issue_key: str, target_status: str) -> None:
        """Transition an issue to the target status by name."""
        resp = await self._client.get(f"/rest/api/3/issue/{issue_key}/transitions")
        resp.raise_for_status()

        transitions = JiraTransitionsResponse.model_validate(resp.json())
        match = next(
            (t for t in transitions.transitions if t.name.lower() == target_status.lower()),
            None,
        )
        if not match:
            available = [t.name for t in transitions.transitions]
            raise ValueError(
                f"No transition to '{target_status}' for {issue_key}. "
                f"Available: {available}"
            )

        resp = await self._client.post(
            f"/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": match.id}},
        )
        resp.raise_for_status()
        await log.ainfo("jira_issue_transitioned", issue=issue_key, status=target_status)

    async def add_comment(self, issue_key: str, body: str) -> None:
        """Add a plain-text comment to an issue using ADF format."""
        adf_body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": body}],
                    }
                ],
            }
        }
        resp = await self._client.post(
            f"/rest/api/3/issue/{issue_key}/comment",
            json=adf_body,
        )
        resp.raise_for_status()
        await log.ainfo("jira_comment_added", issue=issue_key)
