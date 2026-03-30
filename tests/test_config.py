"""Tests for configuration loading."""

import pytest
from pydantic import ValidationError

from gitlab_copilot_agent.config import Settings
from tests.conftest import (
    AZURITE_CONNECTION_STRING,
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
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", AZURITE_CONNECTION_STRING)

    settings = Settings()

    assert settings.gitlab_url == GITLAB_URL
    assert settings.gitlab_token == GITLAB_TOKEN
    assert settings.gitlab_webhook_secret == WEBHOOK_SECRET


def test_settings_loads_without_webhook_secret() -> None:
    """Webhook secret is optional for polling-only mode."""
    settings = make_settings(
        gitlab_webhook_secret=None, gitlab_poll=True, gitlab_projects="group/project"
    )
    assert settings.gitlab_webhook_secret is None


def test_settings_rejects_no_ingestion_path() -> None:
    """Must have at least one event path: webhook secret or polling."""
    with pytest.raises(ValidationError, match="GITLAB_WEBHOOK_SECRET is required"):
        make_settings(gitlab_webhook_secret=None)


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
        Settings()


def test_settings_requires_auth() -> None:
    """github_token or copilot_provider_type must be set."""
    with pytest.raises(ValidationError, match="No LLM authentication configured"):
        Settings(
            gitlab_url=GITLAB_URL,
            gitlab_token=GITLAB_TOKEN,
            gitlab_webhook_secret=WEBHOOK_SECRET,
        )


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
        jira_in_review_status="QA Review",
        jira_poll_interval=60,
    )

    assert settings.jira is not None
    assert settings.jira.trigger_status == "Ready for AI"
    assert settings.jira.in_progress_status == "AI Working"
    assert settings.jira.in_review_status == "QA Review"
    assert settings.jira.poll_interval == 60


def test_k8s_executor_accepts_settings() -> None:
    """task_executor=kubernetes accepts valid configuration."""
    settings = make_settings(task_executor="kubernetes")
    assert settings.task_executor == "kubernetes"


# -- Azure Container Apps executor config tests --

ACA_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
ACA_RESOURCE_GROUP = "rg-test"
ACA_JOB_NAME = "copilot-job"


def test_aca_executor_requires_azure_settings() -> None:
    """task_executor=container_apps fails without required Azure settings."""
    with pytest.raises(ValidationError, match="ACA_SUBSCRIPTION_ID"):
        make_settings(task_executor="container_apps")


def test_aca_executor_accepts_valid_config() -> None:
    """task_executor=container_apps succeeds with all required settings."""
    settings = make_settings(
        task_executor="container_apps",
        aca_subscription_id=ACA_SUBSCRIPTION_ID,
        aca_resource_group=ACA_RESOURCE_GROUP,
        aca_job_name=ACA_JOB_NAME,
    )
    assert settings.aca_subscription_id == ACA_SUBSCRIPTION_ID
    assert settings.aca_job_timeout == 600


# -- Azure Storage dispatch backend config tests --

STORAGE_ACCOUNT_URL = "https://sttest.blob.core.windows.net"
STORAGE_QUEUE_URL = "https://sttest.queue.core.windows.net"


def test_azure_storage_backend_requires_urls() -> None:
    """dispatch_backend=azure_storage fails without storage URLs."""
    with pytest.raises(ValidationError, match="AZURE_STORAGE_ACCOUNT_URL"):
        make_settings(azure_storage_connection_string=None)


def test_azure_storage_backend_accepts_valid_config() -> None:
    """dispatch_backend=azure_storage succeeds with required URLs."""
    settings = make_settings(
        azure_storage_account_url=STORAGE_ACCOUNT_URL,
        azure_storage_queue_url=STORAGE_QUEUE_URL,
    )
    assert settings.dispatch_backend == "azure_storage"
    assert settings.task_queue_name == "task-queue"
    assert settings.task_blob_container == "task-data"


def test_azure_storage_backend_accepts_connection_string() -> None:
    """dispatch_backend=azure_storage succeeds with connection string."""
    settings = make_settings()
    assert settings.dispatch_backend == "azure_storage"
    assert settings.azure_storage_connection_string is not None


class TestPrintConfigErrors:
    """Tests for the human-friendly startup error formatter."""

    def test_missing_field_shown_with_env_var_and_description(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from gitlab_copilot_agent.main import _print_config_errors

        try:
            Settings()
        except ValidationError as exc:
            _print_config_errors(exc)

        err = capsys.readouterr().err
        assert "GITLAB_URL" in err
        assert "GITLAB_TOKEN" in err
        assert "configuration-reference.md" in err

    def test_value_error_shown_as_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        from gitlab_copilot_agent.main import _print_config_errors

        try:
            Settings(
                gitlab_url=GITLAB_URL,
                gitlab_token=GITLAB_TOKEN,
                gitlab_webhook_secret=WEBHOOK_SECRET,
            )
        except ValidationError as exc:
            _print_config_errors(exc)

        err = capsys.readouterr().err
        assert "No LLM authentication configured" in err
        assert "GITHUB_TOKEN" in err
        assert "COPILOT_PROVIDER_TYPE" in err


# -- Plugin config tests --


def test_plugin_defaults_empty() -> None:
    """Plugin fields default to empty lists."""
    settings = make_settings()
    assert settings.copilot_plugins == []
    assert settings.copilot_plugin_marketplaces == []


def test_plugins_from_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """COPILOT_PLUGINS accepts comma-separated values."""
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", AZURITE_CONNECTION_STRING)
    monkeypatch.setenv("COPILOT_PLUGINS", "plugin-a, plugin-b")
    settings = Settings()
    assert settings.copilot_plugins == ["plugin-a", "plugin-b"]


def test_plugins_from_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """COPILOT_PLUGINS accepts JSON array format."""
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", AZURITE_CONNECTION_STRING)
    monkeypatch.setenv("COPILOT_PLUGINS", '["plugin-a","plugin-b"]')
    settings = Settings()
    assert settings.copilot_plugins == ["plugin-a", "plugin-b"]


def test_empty_plugins_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty COPILOT_PLUGINS env var yields empty list."""
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", AZURITE_CONNECTION_STRING)
    monkeypatch.setenv("COPILOT_PLUGINS", "")
    settings = Settings()
    assert settings.copilot_plugins == []


def test_marketplaces_from_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """COPILOT_PLUGIN_MARKETPLACES accepts comma-separated values."""
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", AZURITE_CONNECTION_STRING)
    monkeypatch.setenv(
        "COPILOT_PLUGIN_MARKETPLACES", "https://mp1.example.com,https://mp2.example.com"
    )
    settings = Settings()
    assert settings.copilot_plugin_marketplaces == [
        "https://mp1.example.com",
        "https://mp2.example.com",
    ]
