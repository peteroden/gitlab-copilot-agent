"""Task runner settings — minimal config for queue-based worker jobs."""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from gitlab_copilot_agent.config.base import (
    CopilotSettingsMixin,
    DispatchSettingsMixin,
    PromptSettingsMixin,
)


class TaskRunnerSettings(  # pyright: ignore[reportIncompatibleVariableOverride]
    CopilotSettingsMixin, PromptSettingsMixin, DispatchSettingsMixin, BaseSettings
):
    """Minimal settings for the task runner job.

    Unlike the full ``Settings``, this has no controller-specific validations
    (webhook secret, polling projects, k8s/ACA executor config).  The task
    runner only needs Copilot/LLM credentials and prompt configuration.
    It receives the repo via blob transfer — no GitLab credentials needed.
    """

    model_config = {"env_prefix": ""}

    # Runtime
    clone_dir: str | None = Field(default=None, description="Base directory for repo clones")
    log_level: str = Field(default="info", description="Log level")

    # Job timeout (must match controller's k8s_job_timeout for visibility calculation)
    k8s_job_timeout: int = Field(default=600, description="Job timeout in seconds")
    queue_visibility_buffer: int = Field(
        default=60, description="Extra seconds added to job timeout for queue visibility"
    )

    @model_validator(mode="after")
    def _check_auth(self) -> "TaskRunnerSettings":
        if not self.github_token and not self.copilot_provider_type:
            raise ValueError(
                "No LLM authentication configured. Set GITHUB_TOKEN or COPILOT_PROVIDER_TYPE."
            )
        return self
