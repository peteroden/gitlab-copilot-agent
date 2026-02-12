"""Tests for configuration loading."""

import pytest
from pydantic import ValidationError

from gitlab_copilot_agent.config import Settings
from tests.conftest import GITLAB_TOKEN, GITLAB_URL, WEBHOOK_SECRET, make_settings


def test_settings_loads_required_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", WEBHOOK_SECRET)

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
