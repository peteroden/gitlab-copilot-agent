"""Application configuration via environment variables."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
    sandbox_method: Literal["bwrap", "docker", "podman", "noop"] = Field(
        default="bwrap",
        description="Process sandbox method: bwrap, docker, podman, or noop",
    )
    sandbox_image: str = Field(
        default="copilot-cli-sandbox:latest",
        description="Container image for docker/podman sandbox",
    )
    clone_dir: str | None = Field(
        default=None,
        description="Base directory for repo clones. Required for Docker DinD "
        "(must be a shared volume). Defaults to system temp.",
    )

    # State backends
    state_backend: Literal["memory", "redis"] = Field(
        default="memory",
        description=(
            "State backend for locks and dedup: memory (single-process) or redis (distributed)"
        ),
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis URL (required when STATE_BACKEND=redis). Example: redis://localhost:6379/0",
    )

    # Jira (all optional — service runs review-only without these)
    jira_url: str | None = Field(default=None, description="Jira instance URL")
    jira_email: str | None = Field(default=None, description="Jira user email")
    jira_api_token: str | None = Field(default=None, description="Jira API token")
    jira_trigger_status: str = Field(default="AI Ready", description="Status that triggers agent")
    jira_in_progress_status: str = Field(default="In Progress", description="Status after pickup")
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
                poll_interval=self.jira_poll_interval,
                project_map_json=self.jira_project_map,
            )
        return None

    @model_validator(mode="after")
    def _check_auth(self) -> "Settings":
        if not self.github_token and not self.copilot_provider_type:
            raise ValueError("Either GITHUB_TOKEN or COPILOT_PROVIDER_TYPE must be set")
        if self.sandbox_method == "docker" and not self.clone_dir:
            raise ValueError(
                "CLONE_DIR is required when SANDBOX_METHOD=docker "
                "(must be a shared volume with the DinD sidecar)"
            )
        if self.state_backend == "redis" and not self.redis_url:
            raise ValueError("REDIS_URL is required when STATE_BACKEND=redis")
        return self
