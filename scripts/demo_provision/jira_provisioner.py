"""Jira provisioner — creates projects and issues via Jira REST API v3."""

from __future__ import annotations

import base64

import httpx
import structlog

log = structlog.get_logger()


def build_client(base_url: str, email: str, api_token: str) -> httpx.Client:
    """Build an httpx client with Jira basic auth."""
    auth_bytes = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": f"Basic {auth_bytes}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30.0,
    )


def get_project(client: httpx.Client, key: str) -> dict | None:
    """Get a Jira project by key. Returns None if not found."""
    resp = client.get(f"/rest/api/3/project/{key}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def create_project(
    client: httpx.Client,
    key: str,
    name: str,
    *,
    lead_account_id: str,
    project_type_key: str = "software",
    project_template_key: str = "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
) -> dict:
    """Create a Jira project. Requires Administer Jira permission."""
    resp = client.post(
        "/rest/api/3/project",
        json={
            "key": key,
            "name": name,
            "projectTypeKey": project_type_key,
            "projectTemplateKey": project_template_key,
            "leadAccountId": lead_account_id,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    log.info("jira_project_created", key=key, id=data.get("id"))
    return data


def get_current_user(client: httpx.Client) -> dict:
    """Get the current authenticated user's info."""
    resp = client.get("/rest/api/3/myself")
    resp.raise_for_status()
    return resp.json()


def _make_adf(text: str) -> dict:
    """Convert plain text to Atlassian Document Format."""
    paragraphs = text.strip().split("\n\n")
    content = []
    for para in paragraphs:
        content.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": para.strip()}],
            }
        )
    return {"type": "doc", "version": 1, "content": content}


def create_issue(
    client: httpx.Client,
    project_key: str,
    summary: str,
    description: str,
    *,
    issue_type: str = "Task",
) -> str:
    """Create a Jira issue. Returns the issue key."""
    resp = client.post(
        "/rest/api/3/issue",
        json={
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "description": _make_adf(description),
                "issuetype": {"name": issue_type},
            }
        },
    )
    resp.raise_for_status()
    issue_key = resp.json()["key"]
    log.info("jira_issue_created", issue=issue_key)
    return issue_key


DEMO_ISSUES = [
    {
        "summary": "Add user authentication endpoint",
        "description": (
            "As a developer, I want a /auth/login endpoint so that users can "
            "authenticate with the blog API.\n\n"
            "Acceptance Criteria:\n"
            "- POST /auth/login accepts email and password\n"
            "- Returns a JWT token on successful authentication\n"
            "- Returns 401 with error message on invalid credentials\n"
            "- Passwords are never logged or returned in responses\n"
            "- Token expiry is configurable via environment variable\n\n"
            "Technical Notes:\n"
            "- Replace the hardcoded API_KEY in auth.py with proper JWT-based auth\n"
            "- Use python-jose or PyJWT for token generation\n"
            "- Add password hashing with bcrypt"
        ),
    },
    {
        "summary": "Fix SQL injection vulnerability in database module",
        "description": (
            "As a security engineer, I want all database queries to use "
            "parameterized statements so that the application is not vulnerable "
            "to SQL injection attacks.\n\n"
            "Acceptance Criteria:\n"
            "- All queries in database.py use parameterized statements (%s placeholders)\n"
            "- No string concatenation or f-strings in SQL queries\n"
            "- Add input validation for user-supplied IDs\n"
            "- Add unit tests that verify parameterized queries are used\n\n"
            "Technical Notes:\n"
            "- See database.py get_post() and get_posts_by_author() — both use "
            "f-string interpolation\n"
            "- Reference the project security patterns in "
            ".github/skills/security-patterns/SKILL.md for approved patterns"
        ),
    },
    {
        "summary": "Add structured error logging across all endpoints",
        "description": (
            "As an operations engineer, I want structured logging on all API "
            "endpoints so that errors are traceable in production.\n\n"
            "Acceptance Criteria:\n"
            "- Replace all print() calls with structured logging (logging module)\n"
            "- Each log entry includes: timestamp, level, endpoint, request_id\n"
            "- Errors include stack traces and request context\n"
            "- Add request ID middleware that tags all logs for a request\n"
            "- Log level is configurable via LOG_LEVEL environment variable\n\n"
            "Technical Notes:\n"
            "- main.py currently uses print() for debugging\n"
            "- See .github/instructions/python.instructions.md for logging conventions\n"
            "- Consider using structlog for structured JSON output"
        ),
    },
]
