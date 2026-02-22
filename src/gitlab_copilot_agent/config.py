"""Application configuration via environment variables."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class JiraSettings(BaseModel):
    """Jira configuration — all optional. Service runs review-only without these."""

    model_config = ConfigDict(strict=True)

    url: str = Field(description="Jira instance URL")
    email: str = Field(description="Jira user email for basic auth")
    api_token: str = Field(description="Jira API token or PAT")
    trigger_status: str = Field(
        default="AI Ready", description="Jira status that triggers the agent"
    )
    in_progress_status: str = Field(
        default="In Progress", description="Status to transition to after pickup"
    )
    in_review_status: str = Field(
        default="In Review", description="Status to transition to after MR creation"
    )
    poll_interval: int = Field(default=30, description="Polling interval in seconds")
    project_map_json: str = Field(
        description="JSON string mapping Jira project keys to GitLab projects"
    )


class Settings(BaseSettings):
    """Service configuration loaded from environment variables."""

    model_config = {"env_prefix": ""}

    # GitLab
    gitlab_url: str = Field(description="GitLab instance URL")
    gitlab_token: str = Field(description="GitLab API private token")
    gitlab_webhook_secret: str = Field(description="Secret for validating webhook payloads")

    # Copilot / LLM
    copilot_model: str = Field(default="gpt-4", description="Model to use for reviews")
    copilot_provider_type: str | None = Field(
        default=None, description="BYOK provider type: 'azure', 'openai', or None for Copilot"
    )
    copilot_provider_base_url: str | None = Field(
        default=None, description="BYOK provider base URL"
    )
    copilot_provider_api_key: str | None = Field(default=None, description="BYOK provider API key")
    github_token: str | None = Field(
        default=None, description="GitHub token for Copilot auth (if not using BYOK)"
    )

    # Server
    host: str = Field(default="0.0.0.0", description="Server bind host")
    port: int = Field(default=8000, description="Server bind port")
    log_level: str = Field(default="info", description="Log level")
    agent_gitlab_username: str | None = Field(
        default=None, description="Agent's GitLab username for loop prevention"
    )
    clone_dir: str | None = Field(
        default=None,
        description="Base directory for repo clones. Defaults to system temp.",
    )

    # Task execution
    task_executor: Literal["local", "kubernetes"] = Field(
        default="local", description="Task executor backend: 'local' or 'k8s'"
    )

    # K8s executor settings (only used when task_executor="kubernetes")
    k8s_namespace: str = Field(default="default", description="Kubernetes namespace for Jobs")
    k8s_job_image: str = Field(default="", description="Docker image for Job pods")
    k8s_job_cpu_limit: str = Field(default="1", description="CPU limit for Job pods")
    k8s_job_memory_limit: str = Field(default="1Gi", description="Memory limit for Job pods")
    k8s_job_timeout: int = Field(default=600, description="Job timeout in seconds")
    k8s_job_host_aliases: str = Field(
        default="", description="JSON-encoded hostAliases for Job pods, e.g. [{ip, hostnames}]"
    )
    k8s_secret_name: str | None = Field(
        default=None, description="K8s Secret name for Job pod credentials"
    )
    k8s_configmap_name: str | None = Field(
        default=None, description="K8s ConfigMap name for Job pod config"
    )

    # State backend
    state_backend: Literal["memory", "redis"] = Field(
        default="memory", description="State backend: 'memory' or 'redis'"
    )
    redis_url: str | None = Field(
        default=None, description="Redis URL (required when STATE_BACKEND=redis)"
    )

    # Project allowlist (optional — scopes webhook and poller)
    gitlab_projects: str | None = Field(
        default=None,
        description="Comma-separated GitLab project paths or IDs to scope webhook and poller",
    )

    # GitLab poller
    gitlab_poll: bool = Field(
        default=False,
        description="Enable GitLab API polling for MR and note discovery",
    )
    gitlab_poll_interval: int = Field(
        default=30,
        description="Polling interval in seconds",
    )

    # Jira (all optional — service runs review-only without these)
    jira_url: str | None = Field(default=None, description="Jira instance URL")
    jira_email: str | None = Field(default=None, description="Jira user email")
    jira_api_token: str | None = Field(default=None, description="Jira API token")
    jira_trigger_status: str = Field(default="AI Ready", description="Status that triggers agent")
    jira_in_progress_status: str = Field(default="In Progress", description="Status after pickup")
    jira_in_review_status: str = Field(default="In Review", description="Status after MR creation")
    jira_poll_interval: int = Field(default=30, description="Poll interval seconds")
    jira_project_map: str | None = Field(
        default=None, description="JSON: Jira project key → GitLab project config"
    )

    @property
    def jira(self) -> JiraSettings | None:
        """Return JiraSettings if all required Jira fields are set, else None."""
        if self.jira_url and self.jira_email and self.jira_api_token and self.jira_project_map:
            return JiraSettings(
                url=self.jira_url,
                email=self.jira_email,
                api_token=self.jira_api_token,
                trigger_status=self.jira_trigger_status,
                in_progress_status=self.jira_in_progress_status,
                in_review_status=self.jira_in_review_status,
                poll_interval=self.jira_poll_interval,
                project_map_json=self.jira_project_map,
            )
        return None

    @field_validator("k8s_job_host_aliases")
    @classmethod
    def _validate_host_aliases(cls, v: str) -> str:
        if not v.strip():
            return v
        try:
            entries = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"K8S_JOB_HOST_ALIASES is not valid JSON: {exc}") from exc
        if not isinstance(entries, list):
            raise ValueError("K8S_JOB_HOST_ALIASES must be a JSON array")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or "ip" not in entry or "hostnames" not in entry:
                raise ValueError(f"K8S_JOB_HOST_ALIASES[{i}] must have 'ip' and 'hostnames' keys")
        return v

    @model_validator(mode="after")
    def _check_auth(self) -> "Settings":
        if not self.github_token and not self.copilot_provider_type:
            raise ValueError("Either GITHUB_TOKEN or COPILOT_PROVIDER_TYPE must be set")
        if self.state_backend == "redis" and not self.redis_url:
            raise ValueError("REDIS_URL is required when STATE_BACKEND=redis")
        if self.gitlab_poll:
            entries = [e.strip() for e in (self.gitlab_projects or "").split(",") if e.strip()]
            if not entries:
                raise ValueError("GITLAB_PROJECTS is required when GITLAB_POLL=true")
        return self

    @model_validator(mode="after")
    def _check_k8s_resources(self) -> "Settings":
        if self.task_executor == "kubernetes" and not self.k8s_secret_name:
            import logging

            logging.getLogger(__name__).warning(
                "K8S_SECRET_NAME not set — Job pod credentials will use plaintext env vars. "
                "Set K8S_SECRET_NAME for secure credential injection via K8s Secrets."
            )
        return self
