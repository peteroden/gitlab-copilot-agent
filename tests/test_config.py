"""Tests for configuration loading."""

import pytest
from pydantic import ValidationError

from gitlab_copilot_agent.config import Settings
from tests.conftest import (
    GITHUB_TOKEN,
    GITLAB_TOKEN,
    GITLAB_URL,
    JIRA_EMAIL,
    JIRA_PROJECT_MAP_JSON,
    JIRA_TOKEN,
    JIRA_URL,
    WEBHOOK_SECRET,
    make_settings,
)


def test_settings_loads_required_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)

    settings = Settings()  # type: ignore[call-arg]

    assert settings.gitlab_url == GITLAB_URL
    assert settings.gitlab_token == GITLAB_TOKEN
    assert settings.gitlab_webhook_secret == WEBHOOK_SECRET


def test_settings_defaults() -> None:
    """Verify optional fields have correct defaults (without requiring env vars)."""
    settings = make_settings()
    assert settings.copilot_model == "gpt-4"
    assert settings.copilot_provider_type is None
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.log_level == "info"


def test_settings_missing_required_raises() -> None:
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_requires_auth() -> None:
    """github_token or copilot_provider_type must be set."""
    with pytest.raises(ValidationError, match="GITHUB_TOKEN or COPILOT_PROVIDER_TYPE"):
        Settings(
            gitlab_url=GITLAB_URL,
            gitlab_token=GITLAB_TOKEN,
            gitlab_webhook_secret=WEBHOOK_SECRET,
        )  # type: ignore[call-arg]


def test_settings_accepts_provider_type_without_github_token() -> None:
    settings = make_settings(github_token=None, copilot_provider_type="openai")
    assert settings.copilot_provider_type == "openai"
    assert settings.github_token is None


def test_jira_property_returns_none_when_not_configured() -> None:
    """When no Jira env vars are set, settings.jira should be None."""
    settings = make_settings()
    assert settings.jira is None


def test_jira_property_returns_none_when_partially_configured() -> None:
    """When only some Jira fields are set, settings.jira should be None."""
    settings = make_settings(jira_url=JIRA_URL, jira_email=JIRA_EMAIL)
    assert settings.jira is None



def test_jira_property_uses_custom_values() -> None:
    """Verify custom Jira config values are honored."""
    settings = make_settings(
        jira_url=JIRA_URL,
        jira_email=JIRA_EMAIL,
        jira_api_token=JIRA_TOKEN,
        jira_project_map=JIRA_PROJECT_MAP_JSON,
        jira_trigger_status="Ready for AI",
        jira_in_progress_status="AI Working",
        jira_poll_interval=60,
    )

    assert settings.jira is not None
    assert settings.jira.trigger_status == "Ready for AI"
    assert settings.jira.in_progress_status == "AI Working"
    assert settings.jira.poll_interval == 60

