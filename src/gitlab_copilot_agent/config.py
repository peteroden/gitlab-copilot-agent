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
    gitlab_webhook_secret: str | None = Field(
        default=None,
        description="Secret for validating webhook payloads (required for webhook mode)",
    )

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

    # System prompts (override or append to built-in defaults)
    system_prompt: str | None = Field(
        default=None, description="Global base prompt prepended to all persona prompts"
    )
    system_prompt_suffix: str | None = Field(
        default=None, description="Appended to global base prompt"
    )
    coding_system_prompt: str | None = Field(
        default=None, description="Full override of coding system prompt"
    )
    coding_system_prompt_suffix: str | None = Field(
        default=None, description="Appended to default coding system prompt"
    )
    review_system_prompt: str | None = Field(
        default=None, description="Full override of review system prompt"
    )
    review_system_prompt_suffix: str | None = Field(
        default=None, description="Appended to default review system prompt"
    )
    mr_comment_system_prompt: str | None = Field(
        default=None, description="Full override of MR comment system prompt"
    )
    mr_comment_system_prompt_suffix: str | None = Field(
        default=None, description="Appended to default MR comment system prompt"
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
    task_executor: Literal["local", "kubernetes", "container_apps"] = Field(
        default="local",
        description="Task executor backend: 'local', 'kubernetes', or 'container_apps'",
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
    k8s_job_instance_label: str = Field(
        default="", description="Helm release instance label for Job pod NetworkPolicy scoping"
    )

    # Azure Container Apps executor settings (only used when task_executor="container_apps")
    aca_subscription_id: str = Field(
        default="", description="Azure subscription ID for Container Apps"
    )
    aca_resource_group: str = Field(
        default="", description="Azure resource group containing the Container Apps Job"
    )
    aca_job_name: str = Field(
        default="", description="Name of the Azure Container Apps Job resource"
    )
    aca_job_timeout: int = Field(
        default=600, description="Container Apps Job execution timeout in seconds"
    )

    # State backend
    state_backend: Literal["memory", "redis"] = Field(
        default="memory", description="State backend: 'memory' or 'redis'"
    )
    redis_url: str | None = Field(
        default=None, description="Redis connection string (local/non-Azure environments)"
    )
    redis_host: str | None = Field(
        default=None, description="Redis hostname for Entra ID auth (Azure environments)"
    )
    redis_port: int = Field(default=6380, description="Redis TLS port (used with redis_host)")
    azure_client_id: str | None = Field(
        default=None, description="Managed identity client ID for DefaultAzureCredential"
    )

    @property
    def redis_configured(self) -> bool:
        """True when Redis connectivity is configured (either URL or Entra host)."""
        return bool(self.redis_url or self.redis_host)

    # Git clone retry
    git_clone_max_retries: int = Field(
        default=3, ge=1, description="Max retry attempts for transient git clone failures"
    )
    git_clone_backoff_base: float = Field(
        default=5.0,
        ge=0,
        description="Base interval in seconds for exponential backoff on clone retry",
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
    gitlab_poll_lookback: int = Field(
        default=60,
        description="Minutes to look back on startup for recent MRs (default: 60)",
    )
    gitlab_review_on_push: bool = Field(
        default=True,
        description="Re-review MRs when new commits are pushed (dedup per commit). "
        "When false, each MR is reviewed only once (dedup per MR).",
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
            raise ValueError(
                "No LLM authentication configured. Set one of:\n"
                "  • GITHUB_TOKEN — GitHub PAT for Copilot LLM access\n"
                "  • COPILOT_PROVIDER_TYPE + COPILOT_PROVIDER_BASE_URL + "
                "COPILOT_PROVIDER_API_KEY — BYOK (Azure OpenAI, OpenAI direct)"
            )
        if self.state_backend == "redis" and not self.redis_configured:
            raise ValueError("REDIS_URL or REDIS_HOST is required when STATE_BACKEND=redis")
        if self.gitlab_poll:
            entries = [e.strip() for e in (self.gitlab_projects or "").split(",") if e.strip()]
            if not entries:
                raise ValueError("GITLAB_PROJECTS is required when GITLAB_POLL=true")
        if not self.gitlab_poll and not self.gitlab_webhook_secret:
            raise ValueError(
                "GITLAB_WEBHOOK_SECRET is required when GITLAB_POLL is not enabled. "
                "Set GITLAB_WEBHOOK_SECRET for webhook mode or GITLAB_POLL=true for polling mode."
            )
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

    @model_validator(mode="after")
    def _check_aca_resources(self) -> "Settings":
        if self.task_executor == "container_apps":
            missing = [
                name
                for name, val in [
                    ("ACA_SUBSCRIPTION_ID", self.aca_subscription_id),
                    ("ACA_RESOURCE_GROUP", self.aca_resource_group),
                    ("ACA_JOB_NAME", self.aca_job_name),
                ]
                if not val
            ]
            if missing:
                raise ValueError(f"Container Apps executor requires: {', '.join(missing)}")
            if not self.redis_configured:
                raise ValueError(
                    "REDIS_URL or REDIS_HOST is required when TASK_EXECUTOR=container_apps "
                    "(used for result passback from job executions)"
                )
        return self
