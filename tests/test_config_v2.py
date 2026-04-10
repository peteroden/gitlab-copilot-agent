"""Tests for config_v2 — GitLab-centric v2 configuration models."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from pydantic import ValidationError

from gitlab_copilot_agent.config_v2 import (
    ConfigDefaults,
    ConfigFile,
    CopilotConfig,
    DispatchConfig,
    GitLabConfig,
    JiraIntegrationConfig,
    PollConfig,
    ProjectConfig,
    ServerConfig,
    load_config_file,
)

if TYPE_CHECKING:
    from pathlib import Path

# -- Constants --

GITLAB_URL = "https://gitlab.example.com"

MINIMAL_CONFIG: dict[str, Any] = {
    "version": 2,
    "gitlab": {"url": GITLAB_URL},
}

FULL_CONFIG_YAML = textwrap.dedent("""\
    version: 2

    gitlab:
      url: https://gitlab.example.com

    dispatch:
      backend: aca
      aca_subscription_id: sub-123
      aca_resource_group: rg-test
      aca_job_name: copilot-task
      aca_job_timeout: 900

    copilot:
      model: gpt-4o
      plugins:
        - "@some/plugin"
      marketplaces:
        - https://private.marketplace.example.com

    server:
      log_level: debug
      shutdown_timeout: 60
      webhook_ip_allowlist:
        - "34.74.90.64/28"
      trusted_proxies:
        - "10.0.0.0/8"

    prompts:
      system: "You are a helpful assistant."
      review_suffix: "Be concise."

    defaults:
      target_branch: develop
      credential_ref: platform
      resolution_behavior: auto-resolve
      webhook: false
      poll:
        enabled: true
        interval: 45
        lookback_minutes: 120
        review_on_push: false

    projects:
      - repo: group/service-a
        credential_ref: default
        integrations:
          - my-jira
        copilot:
          model: gpt-4o
          plugins:
            - "@some/plugin"
          marketplaces:
            - https://private.marketplace.example.com

      - repo: group/internal-tool
        credential_ref: platform
        webhook: true
        poll:
          enabled: false

    integrations:
      - name: my-jira
        type: jira
        project_key: PROJ
        trigger_status: "AI Ready"
        in_progress_status: "In Progress"
        in_review_status: "In Review"
""")


def _full_config_dict() -> dict[str, Any]:
    """Return the full config as a parsed dict."""
    return yaml.safe_load(FULL_CONFIG_YAML)


# -- Sub-model tests (parameterized) --


@pytest.mark.parametrize(
    ("model_cls", "field", "expected"),
    [
        (DispatchConfig, "backend", "local"),
        (DispatchConfig, "k8s_namespace", "default"),
        (DispatchConfig, "aca_job_timeout", 600),
        (CopilotConfig, "model", "gpt-4"),
        (CopilotConfig, "plugins", []),
        (CopilotConfig, "marketplaces", []),
        (ServerConfig, "log_level", "info"),
        (ServerConfig, "clone_dir", None),
        (ServerConfig, "shutdown_timeout", 30),
        (ServerConfig, "webhook_ip_allowlist", []),
        (ServerConfig, "trusted_proxies", []),
        (PollConfig, "enabled", False),
        (PollConfig, "interval", 30),
        (PollConfig, "lookback_minutes", 60),
        (PollConfig, "review_on_push", True),
        (ConfigDefaults, "target_branch", "main"),
        (ConfigDefaults, "credential_ref", "default"),
        (ConfigDefaults, "resolution_behavior", "suggest"),
        (ConfigDefaults, "webhook", True),
    ],
)
def test_model_defaults(model_cls: type[Any], field: str, expected: object) -> None:
    """Each sub-model field has the documented default value."""
    instance = model_cls()
    assert getattr(instance, field) == expected


@pytest.mark.parametrize(
    ("model_cls", "data", "match"),
    [
        (GitLabConfig, {}, "url"),
        (DispatchConfig, {"backend": "invalid"}, "backend"),
        (ServerConfig, {"shutdown_timeout": 0}, "shutdown_timeout"),
        (JiraIntegrationConfig, {"type": "jira"}, "name"),
    ],
    ids=["gitlab-missing-url", "dispatch-bad-backend", "server-zero-timeout", "jira-missing-name"],
)
def test_model_rejects_invalid(model_cls: type[Any], data: dict[str, Any], match: str) -> None:
    """Models reject invalid or missing required fields."""
    with pytest.raises(ValidationError, match=match):
        model_cls.model_validate(data)


def test_project_config_minimal() -> None:
    """ProjectConfig only requires repo; everything else falls back to defaults."""
    proj = ProjectConfig(repo="group/project")
    assert proj.repo == "group/project"
    assert proj.credential_ref is None
    assert proj.integrations == []


def test_jira_integration_defaults() -> None:
    """JiraIntegrationConfig populates Jira status defaults."""
    cfg = JiraIntegrationConfig(name="j", type="jira", project_key="P")
    assert cfg.trigger_status == "AI Ready"
    assert cfg.in_progress_status == "In Progress"
    assert cfg.in_review_status == "In Review"


# -- ConfigFile tests --


class TestConfigFile:
    def test_minimal_config(self) -> None:
        cfg = ConfigFile.model_validate(MINIMAL_CONFIG)
        assert cfg.version == 2
        assert cfg.gitlab.url == GITLAB_URL
        assert cfg.dispatch.backend == "local"
        assert cfg.projects == []

    def test_full_config_yaml(self) -> None:
        cfg = ConfigFile.model_validate(_full_config_dict())
        assert cfg.version == 2
        assert len(cfg.projects) == 2
        assert len(cfg.integrations) == 1
        assert cfg.dispatch.backend == "aca"
        assert cfg.copilot.model == "gpt-4o"
        assert cfg.defaults.target_branch == "develop"

    def test_rejects_unknown_integration_ref(self) -> None:
        data = {
            **MINIMAL_CONFIG,
            "projects": [{"repo": "g/p", "integrations": ["nonexistent"]}],
        }
        with pytest.raises(ValueError, match="unknown integration 'nonexistent'"):
            ConfigFile.model_validate(data)

    def test_rejects_duplicate_repos(self) -> None:
        data = {
            **MINIMAL_CONFIG,
            "projects": [{"repo": "g/p"}, {"repo": "g/p"}],
        }
        with pytest.raises(ValueError, match="Duplicate project repo"):
            ConfigFile.model_validate(data)

    def test_rejects_wrong_version(self) -> None:
        with pytest.raises(ValidationError):
            ConfigFile.model_validate({"version": 1, "gitlab": {"url": GITLAB_URL}})

    def test_json_schema_generation(self) -> None:
        schema = ConfigFile.model_json_schema()
        assert schema["title"] == "ConfigFile"
        assert "properties" in schema
        assert "version" in schema["properties"]

    def test_get_integration(self) -> None:
        cfg = ConfigFile.model_validate(_full_config_dict())
        jira = cfg.get_integration("my-jira")
        assert jira is not None
        assert jira.project_key == "PROJ"

    def test_get_integration_missing(self) -> None:
        cfg = ConfigFile.model_validate(MINIMAL_CONFIG)
        assert cfg.get_integration("nonexistent") is None


class TestResolveProject:
    def test_applies_defaults_for_omitted_fields(self) -> None:
        cfg = ConfigFile.model_validate(
            {
                **MINIMAL_CONFIG,
                "defaults": {"target_branch": "develop", "credential_ref": "team"},
                "projects": [{"repo": "g/p"}],
            }
        )
        resolved = cfg.resolve_project(cfg.projects[0])
        assert resolved["credential_ref"] == "team"
        assert resolved["target_branch"] == "develop"
        assert resolved["resolution_behavior"] == "suggest"
        assert resolved["webhook"] is True

    def test_project_overrides_defaults(self) -> None:
        cfg = ConfigFile.model_validate(
            {
                **MINIMAL_CONFIG,
                "projects": [
                    {
                        "repo": "g/p",
                        "credential_ref": "custom",
                        "target_branch": "release",
                        "resolution_behavior": "auto-resolve",
                        "webhook": False,
                    }
                ],
            }
        )
        resolved = cfg.resolve_project(cfg.projects[0])
        assert resolved["credential_ref"] == "custom"
        assert resolved["target_branch"] == "release"
        assert resolved["resolution_behavior"] == "auto-resolve"
        assert resolved["webhook"] is False

    def test_resolves_integrations(self) -> None:
        cfg = ConfigFile.model_validate(_full_config_dict())
        proj = cfg.projects[0]
        resolved = cfg.resolve_project(proj)
        integrations = resolved["integrations"]
        assert len(integrations) == 1
        assert integrations[0].project_key == "PROJ"

    def test_copilot_override(self) -> None:
        cfg = ConfigFile.model_validate(_full_config_dict())
        # project 0 has copilot override
        resolved = cfg.resolve_project(cfg.projects[0])
        assert resolved["copilot"].model == "gpt-4o"
        # project 1 falls back to global
        resolved2 = cfg.resolve_project(cfg.projects[1])
        assert resolved2["copilot"].model == "gpt-4o"  # global copilot


# -- load_config_file tests --


class TestLoadConfigFile:
    @pytest.mark.parametrize(
        ("content", "expected_projects"),
        [
            (yaml.dump(MINIMAL_CONFIG), 0),
            (FULL_CONFIG_YAML, 2),
        ],
        ids=["minimal", "full"],
    )
    def test_loads_valid_yaml(self, tmp_path: Path, content: str, expected_projects: int) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(content)
        cfg = load_config_file(config_file)
        assert cfg.version == 2
        assert len(cfg.projects) == expected_projects

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config_file(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml_content(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("just a string")
        with pytest.raises(ValueError, match="expected a YAML mapping"):
            load_config_file(config_file)

    def test_reads_config_file_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "custom.yaml"
        config_file.write_text(yaml.dump(MINIMAL_CONFIG))
        monkeypatch.setenv("CONFIG_FILE", str(config_file))
        cfg = load_config_file()
        assert cfg.gitlab.url == GITLAB_URL

    def test_audit_logs_marketplace_urls(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(FULL_CONFIG_YAML)
        load_config_file(config_file)
        captured = capsys.readouterr().out
        assert "marketplace_urls_configured" in captured
        assert "scope=global" in captured
        assert "scope=project" in captured
