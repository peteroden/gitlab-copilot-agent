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


def test_k8s_executor_warns_without_secret_name() -> None:
    """task_executor=kubernetes warns (not errors) when k8s_secret_name is not set."""
    settings = make_settings(task_executor="kubernetes")
    assert settings.k8s_secret_name is None


def test_k8s_executor_accepts_both_names() -> None:
    """task_executor=kubernetes succeeds when both names are provided."""
    settings = make_settings(
        task_executor="kubernetes",
        k8s_secret_name="my-secret",
        k8s_configmap_name="my-configmap",
    )
    assert settings.k8s_secret_name == "my-secret"
    assert settings.k8s_configmap_name == "my-configmap"


def test_local_executor_does_not_require_k8s_names() -> None:
    """task_executor=local does not require k8s_secret_name or k8s_configmap_name."""
    settings = make_settings(task_executor="local")
    assert settings.k8s_secret_name is None
    assert settings.k8s_configmap_name is None


# -- Azure Container Apps executor config tests --

ACA_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
ACA_RESOURCE_GROUP = "rg-test"
ACA_JOB_NAME = "copilot-job"
ACA_REDIS_URL = "rediss://test-redis.redis.cache.windows.net:6380"


def test_aca_executor_requires_azure_settings() -> None:
    """task_executor=container_apps fails without required Azure settings."""
    with pytest.raises(ValidationError, match="ACA_SUBSCRIPTION_ID"):
        make_settings(task_executor="container_apps", redis_url=ACA_REDIS_URL)


def test_aca_executor_requires_redis_url() -> None:
    """task_executor=container_apps requires REDIS_URL for result passback."""
    with pytest.raises(ValidationError, match="REDIS_URL is required"):
        make_settings(
            task_executor="container_apps",
            aca_subscription_id=ACA_SUBSCRIPTION_ID,
            aca_resource_group=ACA_RESOURCE_GROUP,
            aca_job_name=ACA_JOB_NAME,
        )


def test_aca_executor_accepts_valid_config() -> None:
    """task_executor=container_apps succeeds with all required settings."""
    settings = make_settings(
        task_executor="container_apps",
        aca_subscription_id=ACA_SUBSCRIPTION_ID,
        aca_resource_group=ACA_RESOURCE_GROUP,
        aca_job_name=ACA_JOB_NAME,
        redis_url=ACA_REDIS_URL,
        state_backend="redis",
    )
    assert settings.aca_subscription_id == ACA_SUBSCRIPTION_ID
    assert settings.aca_job_timeout == 600


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
