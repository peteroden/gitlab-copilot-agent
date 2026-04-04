"""Application configuration via environment variables."""

import warnings
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
    copilot_plugins: str | list[str] = Field(
        default_factory=list,
        description="Service-level Copilot CLI plugins to install at runtime",
    )
    copilot_plugin_marketplaces: str | list[str] = Field(
        default_factory=list, description="Custom plugin marketplace URLs"
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
    discussion_system_prompt: str | None = Field(
        default=None, description="Full override of discussion system prompt"
    )
    discussion_system_prompt_suffix: str | None = Field(
        default=None, description="Appended to default discussion system prompt"
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
    agent_author_name: str = Field(
        default="Copilot Agent",
        description="Git author name for agent commits",
    )
    agent_author_email: str = Field(
        default="copilot-agent@noreply.gitlab.com",
        description="Git author email for agent commits",
    )
    shutdown_timeout: int = Field(
        default=30, gt=0, description="Graceful shutdown timeout in seconds"
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

    # Dispatch backend (determines queue + result store)
    dispatch_backend: Literal["azure_storage"] = Field(
        default="azure_storage",
        description="Dispatch backend: 'azure_storage' (Queue + Blob via Claim Check)",
    )
    azure_storage_account_url: str | None = Field(
        default=None,
        description="Azure Blob Storage endpoint, e.g. https://<acct>.blob.core.windows.net",
    )
    azure_storage_queue_url: str | None = Field(
        default=None,
        description="Azure Queue Storage endpoint, e.g. https://<acct>.queue.core.windows.net",
    )
    azure_storage_connection_string: str | None = Field(
        default=None,
        description="Azure Storage connection string (for Azurite/K8s); overrides URL-based auth",
    )
    task_queue_name: str = Field(
        default="task-queue", description="Azure Storage Queue name for task dispatch"
    )
    task_blob_container: str = Field(
        default="task-data", description="Azure Blob container for params and results"
    )

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

    @field_validator("copilot_plugins", "copilot_plugin_marketplaces", mode="before")
    @classmethod
    def _parse_comma_list(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            # Try JSON first, fall back to comma-separated
            if v.startswith("["):
                return v  # let pydantic handle JSON
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

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

    @model_validator(mode="after")
    def _warn_deprecated_fields(self) -> "Settings":
        if self.agent_gitlab_username is not None:
            warnings.warn(
                "agent_gitlab_username is deprecated. "
                "Agent identity is now auto-discovered via GET /user. "
                "Remove AGENT_GITLAB_USERNAME from your configuration.",
                DeprecationWarning,
                stacklevel=2,
            )
        return self

    @model_validator(mode="after")
    def _check_auth(self) -> "Settings":
        if not self.github_token and not self.copilot_provider_type:
            raise ValueError(
                "No LLM authentication configured. Set one of:\n"
                "  • GITHUB_TOKEN — GitHub PAT for Copilot LLM access\n"
                "  • COPILOT_PROVIDER_TYPE + COPILOT_PROVIDER_BASE_URL + "
                "COPILOT_PROVIDER_API_KEY — BYOK (Azure OpenAI, OpenAI direct)"
            )
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
        return self

    @model_validator(mode="after")
    def _check_azure_storage(self) -> "Settings":
        if self.dispatch_backend == "azure_storage" and not self.azure_storage_connection_string:
            missing = [
                name
                for name, val in [
                    ("AZURE_STORAGE_ACCOUNT_URL", self.azure_storage_account_url),
                    ("AZURE_STORAGE_QUEUE_URL", self.azure_storage_queue_url),
                ]
                if not val
            ]
            if missing:
                raise ValueError(
                    f"dispatch_backend='azure_storage' requires: {', '.join(missing)} "
                    f"(or set AZURE_STORAGE_CONNECTION_STRING)"
                )
        return self


class TaskRunnerSettings(BaseSettings):
    """Minimal settings for the task runner job.

    Unlike the full ``Settings``, this has no controller-specific validations
    (webhook secret, polling projects, k8s/ACA executor config).  The task
    runner only needs Copilot/LLM credentials and prompt configuration.
    It receives the repo via blob transfer — no GitLab credentials needed.
    """

    model_config = {"env_prefix": ""}

    # Copilot / LLM
    copilot_model: str = Field(default="gpt-4", description="Model to use")
    copilot_provider_type: str | None = Field(default=None, description="BYOK provider type")
    copilot_provider_base_url: str | None = Field(default=None, description="BYOK provider URL")
    copilot_provider_api_key: str | None = Field(default=None, description="BYOK provider API key")
    github_token: str | None = Field(default=None, description="GitHub token for Copilot auth")
    copilot_plugins: str | list[str] = Field(
        default_factory=list,
        description="Service-level Copilot CLI plugins to install at runtime",
    )
    copilot_plugin_marketplaces: str | list[str] = Field(
        default_factory=list, description="Custom plugin marketplace URLs"
    )

    # System prompts
    system_prompt: str | None = Field(default=None, description="Global base prompt")
    system_prompt_suffix: str | None = Field(default=None, description="Appended to global base")
    coding_system_prompt: str | None = Field(default=None, description="Coding prompt override")
    coding_system_prompt_suffix: str | None = Field(default=None, description="Coding suffix")
    review_system_prompt: str | None = Field(default=None, description="Review prompt override")
    review_system_prompt_suffix: str | None = Field(default=None, description="Review suffix")
    discussion_system_prompt: str | None = Field(
        default=None, description="Discussion prompt override"
    )
    discussion_system_prompt_suffix: str | None = Field(
        default=None, description="Discussion suffix"
    )

    # Runtime
    clone_dir: str | None = Field(default=None, description="Base directory for repo clones")
    log_level: str = Field(default="info", description="Log level")

    # Azure Storage (for queue-based dispatch)
    dispatch_backend: Literal["azure_storage"] = Field(
        default="azure_storage", description="Dispatch backend"
    )
    azure_storage_account_url: str | None = Field(
        default=None, description="Azure Blob Storage endpoint"
    )
    azure_storage_queue_url: str | None = Field(
        default=None, description="Azure Queue Storage endpoint"
    )
    azure_storage_connection_string: str | None = Field(
        default=None, description="Azure Storage connection string (for Azurite/K8s)"
    )
    task_queue_name: str = Field(default="task-queue", description="Queue name")
    task_blob_container: str = Field(default="task-data", description="Blob container name")

    # Job timeout (must match controller's k8s_job_timeout for visibility calculation)
    k8s_job_timeout: int = Field(default=600, description="Job timeout in seconds")
    queue_visibility_buffer: int = Field(
        default=60, description="Extra seconds added to job timeout for queue visibility"
    )

    @field_validator("copilot_plugins", "copilot_plugin_marketplaces", mode="before")
    @classmethod
    def _parse_comma_list(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            # Try JSON first, fall back to comma-separated
            if v.startswith("["):
                return v  # let pydantic handle JSON
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @model_validator(mode="after")
    def _check_auth(self) -> "TaskRunnerSettings":
        if not self.github_token and not self.copilot_provider_type:
            raise ValueError(
                "No LLM authentication configured. Set GITHUB_TOKEN or COPILOT_PROVIDER_TYPE."
            )
        return self
