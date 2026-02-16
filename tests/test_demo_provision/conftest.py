"""Shared constants and fixtures for demo provisioner tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "scripts" / "demo_templates" / "blog-api"


@pytest.fixture()
def mock_gl() -> MagicMock:
    """Mock python-gitlab Gitlab instance."""
    gl = MagicMock()
    return gl


@pytest.fixture()
def mock_project() -> MagicMock:
    """Mock python-gitlab Project."""
    project = MagicMock()
    project.id = 42
    project.path_with_namespace = GITLAB_PROJECT_PATH
    project.web_url = GITLAB_PROJECT_URL
    return project


@pytest.fixture()
def demo_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all required environment variables for demo provisioning."""
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("JIRA_URL", JIRA_URL)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_API_TOKEN)
