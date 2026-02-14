"""Shared test constants, fixtures, and factory functions."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.gitlab_client import MRChange, MRDiffRef
from gitlab_copilot_agent.main import app

# -- Constants --

GITLAB_URL = "https://gitlab.example.com"
GITLAB_TOKEN = "test-token"
WEBHOOK_SECRET = "test-secret"
HEADERS = {"X-Gitlab-Token": WEBHOOK_SECRET}

# Jira constants
JIRA_URL = "https://jira.example.com"
JIRA_EMAIL = "bot@example.com"
JIRA_TOKEN = "test-jira-token"
JIRA_PROJECT_MAP_JSON = (
    '{"mappings": {"PROJ": {"gitlab_project_id": 42, '
    '"clone_url": "https://gitlab.example.com/group/project.git", '
    '"target_branch": "main"}}}'
)

PROJECT_ID = 42
MR_IID = 7

DIFF_REFS = MRDiffRef(base_sha="aaa", start_sha="bbb", head_sha="ccc")

# Sample unified diff for testing position validation
SAMPLE_DIFF = """@@ -1,3 +1,4 @@
 import sys
+import os

 def main():
@@ -10,2 +11,3 @@ def helper():
     return x
+    # TODO: refactor
"""

MR_PAYLOAD: dict[str, Any] = {
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "jdoe"},
    "project": {
        "id": PROJECT_ID,
        "path_with_namespace": "group/my-project",
        "git_http_url": "https://gitlab.com/group/my-project.git",
    },
    "object_attributes": {
        "iid": MR_IID,
        "title": "Add feature",
        "description": "Implements X",
        "action": "open",
        "source_branch": "feature/x",
        "target_branch": "main",
        "last_commit": {"id": "abc123", "message": "feat: add X"},
        "url": "https://gitlab.com/group/my-project/-/merge_requests/7",
    },
}

FAKE_REVIEW_OUTPUT = (
    "```json\n"
    '[{"file": "src/main.py", "line": 10, "severity": "warning", '
    '"comment": "Consider error handling here"}]\n'
    "```\n"
    "Overall the changes look reasonable."
)


# -- Factories --


def make_settings(**overrides: Any) -> Settings:
    """Create a Settings instance with test defaults. Override any field."""
    defaults: dict[str, Any] = {
        "gitlab_url": GITLAB_URL,
        "gitlab_token": GITLAB_TOKEN,
        "gitlab_webhook_secret": WEBHOOK_SECRET,
        "github_token": "gho_test_token",
    }
    return Settings(**(defaults | overrides))  # type: ignore[call-arg]


def make_mr_payload(**attr_overrides: Any) -> dict[str, Any]:
    """Create an MR webhook payload. Override object_attributes fields."""
    payload: dict[str, Any] = {**MR_PAYLOAD}
    if attr_overrides:
        payload["object_attributes"] = {**payload["object_attributes"], **attr_overrides}
    return payload


def make_mr_changes(file_path: str = "src/main.py", diff: str = SAMPLE_DIFF) -> list[MRChange]:
    """Create sample MRChange list for testing."""
    return [
        MRChange(
            old_path=file_path,
            new_path=file_path,
            diff=diff,
            new_file=False,
            deleted_file=False,
            renamed_file=False,
        )
    ]


# -- Fixtures --


@pytest.fixture
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set required env vars for Settings."""
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", WEBHOOK_SECRET)


@pytest.fixture
async def client(env_vars: None) -> AsyncIterator[AsyncClient]:
    """AsyncClient wired to the FastAPI app with test settings."""
    app.state.settings = make_settings()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
