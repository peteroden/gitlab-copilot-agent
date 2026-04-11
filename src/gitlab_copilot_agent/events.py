"""Internal event models — unified trigger → pipeline contract.

All trigger paths (webhook, GitLab poller, Jira poller) produce ``TaskEvent``;
orchestrators and future pipelines consume it.  Replaces direct webhook
payload passing and eliminates synthetic payload construction in pollers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gitlab_copilot_agent.mapping_models import (
    ResolutionBehavior,  # noqa: TC001 — Pydantic runtime
)

if TYPE_CHECKING:
    from collections.abc import Iterator

TriggerSource = Literal["webhook", "gitlab_poller", "jira_poller"]
TaskType = Literal["review", "discussion", "coding"]


class TaskEvent(BaseModel):
    """Unified internal event from any trigger.

    Carries enough context for dedup, scheduling, and pipeline execution.
    Frozen after construction — treat as immutable value object.
    """

    model_config = ConfigDict(frozen=True)

    task_type: TaskType = Field(description="Pipeline to invoke")
    project_id: int = Field(description="GitLab numeric project ID")
    repo: str = Field(description="GitLab repo path_with_namespace")
    clone_url: str = Field(description="Git HTTP clone URL")
    branch: str = Field(description="Source branch to review/work on")
    target_branch: str = Field(description="Target branch for MR")
    mr_iid: int | None = Field(
        default=None, description="MR IID (None for coding tasks without MR)"
    )
    head_sha: str | None = Field(default=None, description="HEAD commit SHA for dedup")
    trigger_source: TriggerSource = Field(description="Which trigger produced this event")
    token: str = Field(
        description="GitLab token for this project",
        repr=False,
        exclude=True,
    )
    credential_ref: str = Field(default="default", description="Credential registry key")
    resolution_behavior: ResolutionBehavior = Field(
        default="suggest", description="Resolution behavior for this project"
    )

    # Discussion-specific fields
    note_id: int | None = Field(default=None, description="Triggering note ID (discussion events)")
    discussion_id: str | None = Field(
        default=None, description="Discussion ID (discussion events)"
    )
    note_body: str | None = Field(default=None, description="Note body text (discussion events)")

    # Coding-specific fields
    jira_issue_key: str | None = Field(default=None, description="Jira issue key (coding events)")

    @model_validator(mode="after")
    def _validate_task_fields(self) -> TaskEvent:
        """Enforce required fields per task type to prevent invalid states."""
        if self.task_type == "review":
            if self.mr_iid is None:
                msg = "review events require mr_iid"
                raise ValueError(msg)
            if self.head_sha is None:
                msg = "review events require head_sha"
                raise ValueError(msg)
        elif self.task_type == "discussion":
            if self.mr_iid is None:
                msg = "discussion events require mr_iid"
                raise ValueError(msg)
            if self.note_id is None:
                msg = "discussion events require note_id"
                raise ValueError(msg)
        return self

    def log_safe(self) -> dict[str, object]:
        """Return a dict safe for structured logging — no secrets.

        Token is already excluded by ``Field(exclude=True)``, but this
        method provides defense-in-depth by explicitly removing it.
        """
        d = self.model_dump()
        d.pop("token", None)
        return d

    def __iter__(self) -> Iterator[tuple[str, object]]:  # type: ignore[override]
        """Prevent token leakage via dict(event) or **event."""
        yield from self.model_dump().items()


class ScheduledTask(BaseModel):
    """Deduped, enriched task ready for pipeline execution.

    Produced by the scheduler/dedup layer after the duplicate check passes.
    """

    model_config = ConfigDict(frozen=True)

    event: TaskEvent = Field(description="Original trigger event")
    dedup_key: str = Field(description="Key used for dedup check")
    trace_id: str = Field(default="", description="W3C trace ID for cross-boundary propagation")
