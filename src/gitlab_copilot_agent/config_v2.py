"""Config v2 models — GitLab-project-centric YAML configuration.

Full service configuration: service-level settings, per-project config
with trigger and copilot overrides, and pluggable integrations.
Replaces both mapping_models.py (Jira-keyed bindings) and the non-secret
portions of config.py (Settings).

See architecture plan S4 and ADR-0010.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

log = structlog.get_logger()

ResolutionBehavior = Literal["auto-resolve", "suggest", "off"]

DEFAULT_CONFIG_PATH = "config.yaml"


class GitLabConfig(BaseModel):
    """GitLab instance configuration."""

    model_config = ConfigDict(strict=True)

    url: str = Field(description="GitLab instance URL")


class DispatchConfig(BaseModel):
    """Task dispatch backend configuration."""

    model_config = ConfigDict(strict=True)

    backend: Literal["local", "k8s", "aca"] = Field(
        default="local", description="Dispatch backend"
    )
    # K8s-specific
    k8s_namespace: str = Field(default="default", description="K8s namespace for Jobs")
    k8s_job_image: str = Field(default="", description="Docker image for Job pods")
    k8s_job_timeout: int = Field(default=600, description="Job timeout in seconds")
    # ACA-specific
    aca_subscription_id: str = Field(default="", description="Azure subscription ID")
    aca_resource_group: str = Field(default="", description="Azure resource group")
    aca_job_name: str = Field(default="", description="ACA Job resource name")
    aca_job_timeout: int = Field(default=600, description="ACA Job timeout in seconds")


class CopilotConfig(BaseModel):
    """Copilot/LLM configuration — global defaults, overridable per-project."""

    model_config = ConfigDict(strict=True)

    model: str = Field(default="gpt-4", description="Default model for Copilot sessions")
    plugins: list[str] = Field(default_factory=list, description="Copilot CLI plugins")
    marketplaces: list[str] = Field(
        default_factory=list, description="Custom plugin marketplace URLs"
    )


class ServerConfig(BaseModel):
    """Server operational configuration."""

    model_config = ConfigDict(strict=True)

    log_level: str = Field(default="info", description="Log level")
    clone_dir: str | None = Field(default=None, description="Base directory for repo clones")
    shutdown_timeout: int = Field(
        default=30, gt=0, description="Graceful shutdown timeout in seconds"
    )
    webhook_ip_allowlist: list[str] = Field(
        default_factory=list,
        description="CIDR ranges allowed to send webhooks. Empty = allow all. "
        "GitLab.com ranges: 34.74.90.64/28, 34.74.226.0/24",
    )
    trusted_proxies: list[str] = Field(
        default_factory=list,
        description="CIDR ranges of trusted reverse proxies for X-Forwarded-For parsing",
    )


class PromptsConfig(BaseModel):
    """System prompt overrides and suffixes."""

    model_config = ConfigDict(strict=True)

    system: str | None = Field(default=None, description="Global base prompt override")
    system_suffix: str | None = Field(default=None, description="Appended to global base")
    review: str | None = Field(default=None, description="Review prompt override")
    review_suffix: str | None = Field(default=None, description="Appended to review prompt")
    coding: str | None = Field(default=None, description="Coding prompt override")
    coding_suffix: str | None = Field(default=None, description="Appended to coding prompt")
    discussion: str | None = Field(default=None, description="Discussion prompt override")
    discussion_suffix: str | None = Field(
        default=None, description="Appended to discussion prompt"
    )


class PollConfig(BaseModel):
    """GitLab polling configuration."""

    model_config = ConfigDict(strict=True)

    enabled: bool = Field(default=False, description="Enable GitLab API polling")
    interval: int = Field(default=30, description="Polling interval in seconds")
    lookback_minutes: int = Field(
        default=60, description="Minutes to look back on startup for recent MRs"
    )
    review_on_push: bool = Field(
        default=True,
        description="Re-review MRs on new commits (dedup per commit). "
        "When false, each MR reviewed once (dedup per MR).",
    )


class ConfigDefaults(BaseModel):
    """Default values applied to every project unless overridden."""

    model_config = ConfigDict(strict=True)

    target_branch: str = Field(default="main", description="Default MR target branch")
    credential_ref: str = Field(default="default", description="Default credential alias")
    resolution_behavior: ResolutionBehavior = Field(
        default="suggest", description="Default resolution behavior"
    )
    webhook: bool = Field(default=True, description="Default webhook trigger enabled")
    poll: PollConfig = Field(
        default_factory=PollConfig, description="Default polling configuration"
    )


class ProjectConfig(BaseModel):
    """A single GitLab project entry."""

    model_config = ConfigDict(strict=True)

    repo: str = Field(description="GitLab repo path_with_namespace (e.g. group/repo)")
    credential_ref: str | None = Field(
        default=None, description="Credential alias; falls back to defaults"
    )
    target_branch: str | None = Field(
        default=None, description="Target branch; falls back to defaults"
    )
    resolution_behavior: ResolutionBehavior | None = Field(
        default=None, description="Resolution behavior; falls back to defaults"
    )
    webhook: bool | None = Field(
        default=None, description="Webhook trigger; falls back to defaults"
    )
    poll: PollConfig | None = Field(
        default=None, description="Polling config; falls back to defaults"
    )
    copilot: CopilotConfig | None = Field(
        default=None, description="Per-project Copilot overrides"
    )
    integrations: list[str] = Field(
        default_factory=list,
        description="Integration names from the integrations list",
    )


class JiraIntegrationConfig(BaseModel):
    """Jira integration block."""

    model_config = ConfigDict(strict=True)

    name: str = Field(description="Unique integration name, referenced by projects")
    type: Literal["jira"] = Field(description="Integration type discriminator")
    project_key: str = Field(description="Jira project key (e.g. PROJ)")
    trigger_status: str = Field(
        default="AI Ready", description="Jira status that triggers the agent"
    )
    in_progress_status: str = Field(default="In Progress", description="Status after agent pickup")
    in_review_status: str = Field(default="In Review", description="Status after MR creation")


IntegrationConfig = JiraIntegrationConfig  # Union when more types added


class ConfigFile(BaseModel):
    """Root config file model (version 2).

    The YAML file is the primary non-secret config source. Secrets
    (tokens, connection strings) stay as env vars. A few operational
    env vars (LOG_LEVEL, DISPATCH_BACKEND) can override YAML values.
    """

    model_config = ConfigDict(strict=True)

    version: Literal[2] = Field(description="Schema version — must be 2")
    gitlab: GitLabConfig = Field(description="GitLab instance configuration")
    dispatch: DispatchConfig = Field(
        default_factory=DispatchConfig, description="Dispatch backend config"
    )
    copilot: CopilotConfig = Field(
        default_factory=CopilotConfig, description="Global Copilot defaults"
    )
    server: ServerConfig = Field(
        default_factory=ServerConfig, description="Server operational config"
    )
    prompts: PromptsConfig = Field(default_factory=PromptsConfig, description="Prompt overrides")
    defaults: ConfigDefaults = Field(
        default_factory=ConfigDefaults, description="Project defaults"
    )
    projects: list[ProjectConfig] = Field(
        default_factory=lambda: [], description="GitLab project definitions"
    )
    integrations: list[IntegrationConfig] = Field(
        default_factory=lambda: [], description="Named integration configurations"
    )

    @model_validator(mode="after")
    def _validate_integration_refs(self) -> ConfigFile:
        """Ensure every project.integrations ref points to a defined integration."""
        known = {i.name for i in self.integrations}
        for proj in self.projects:
            for ref in proj.integrations:
                if ref not in known:
                    msg = (
                        f"Project '{proj.repo}' references unknown integration '{ref}'. "
                        f"Known: {sorted(known)}"
                    )
                    raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_unique_repos(self) -> ConfigFile:
        """Ensure no duplicate project repos."""
        seen: dict[str, int] = {}
        for idx, proj in enumerate(self.projects):
            if proj.repo in seen:
                msg = f"Duplicate project repo '{proj.repo}' at index {seen[proj.repo]} and {idx}"
                raise ValueError(msg)
            seen[proj.repo] = idx
        return self

    def resolve_project(self, project: ProjectConfig) -> dict[str, object]:
        """Apply defaults to a project and return resolved values.

        Args:
            project: A project config entry to resolve against defaults.

        Returns:
            Dict with fully resolved values for all project fields.
        """
        return {
            "repo": project.repo,
            "credential_ref": project.credential_ref or self.defaults.credential_ref,
            "target_branch": project.target_branch or self.defaults.target_branch,
            "resolution_behavior": (
                project.resolution_behavior or self.defaults.resolution_behavior
            ),
            "webhook": (project.webhook if project.webhook is not None else self.defaults.webhook),
            "poll": project.poll or self.defaults.poll,
            "copilot": project.copilot or self.copilot,
            "integrations": [self.get_integration(name) for name in project.integrations],
        }

    def get_integration(self, name: str) -> IntegrationConfig | None:
        """Look up an integration by name.

        Args:
            name: Integration name as referenced in project configs.

        Returns:
            The matching IntegrationConfig, or None if not found.
        """
        for i in self.integrations:
            if i.name == name:
                return i
        return None


def load_config_file(path: Path | None = None) -> ConfigFile:
    """Load and validate a v2 config YAML file.

    Args:
        path: Path to the YAML config file. If None, reads from
            CONFIG_FILE env var (default: config.yaml).

    Returns:
        Validated ConfigFile model.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the file is not valid YAML or fails validation.
    """
    if path is None:
        path = Path(os.environ.get("CONFIG_FILE", DEFAULT_CONFIG_PATH))
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        msg = f"{path}: expected a YAML mapping, got {type(raw).__name__}"
        raise ValueError(msg)
    config = ConfigFile.model_validate(raw)
    _audit_log_marketplaces(config)
    return config


def _audit_log_marketplaces(config: ConfigFile) -> None:
    """S10: Emit structured audit logs for configured marketplace URLs.

    Marketplace URLs are external trust boundaries — operators must be aware
    of any configured custom marketplaces at startup and on reload.
    """
    if config.copilot.marketplaces:
        log.info(
            "marketplace_urls_configured",
            scope="global",
            urls=config.copilot.marketplaces,
        )
    for proj in config.projects:
        if proj.copilot and proj.copilot.marketplaces:
            log.info(
                "marketplace_urls_configured",
                scope="project",
                repo=proj.repo,
                urls=proj.copilot.marketplaces,
            )
