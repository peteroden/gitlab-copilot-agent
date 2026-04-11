"""Shared field groups used by both Settings and TaskRunnerSettings."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from gitlab_copilot_agent.config.validators import parse_comma_list


class CopilotSettingsMixin(BaseModel):
    """Copilot/LLM fields shared between controller and task runner."""

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

    @field_validator("copilot_plugins", "copilot_plugin_marketplaces", mode="before")
    @classmethod
    def _parse_comma_list(cls, v: object) -> object:
        return parse_comma_list(v)


class PromptSettingsMixin(BaseModel):
    """System prompt fields shared between controller and task runner."""

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


class DispatchSettingsMixin(BaseModel):
    """Dispatch/storage fields shared between controller and task runner."""

    dispatch_backend: Literal["azure_storage", "local"] = Field(
        default="azure_storage",
        description="Dispatch backend: 'azure_storage' (Queue + Blob) or 'local' (in-memory)",
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
