"""Pydantic models for GitLab webhook payloads."""

import time

from pydantic import BaseModel, ConfigDict, Field


class WebhookUser(BaseModel):
    model_config = ConfigDict(strict=True)

    id: int
    username: str


class WebhookProject(BaseModel):
    model_config = ConfigDict(strict=True)

    id: int = Field(description="Numeric project ID for API calls")
    path_with_namespace: str
    git_http_url: str


class MRLastCommit(BaseModel):
    model_config = ConfigDict(strict=True)

    id: str = Field(description="Commit SHA")
    message: str


class MRObjectAttributes(BaseModel):
    model_config = ConfigDict(strict=True)

    iid: int = Field(description="MR number within the project")
    title: str
    description: str | None = None
    action: str = Field(description="Trigger action: open, update, merge, close, etc.")
    source_branch: str
    target_branch: str
    last_commit: MRLastCommit
    url: str
    oldrev: str | None = Field(
        default=None,
        description="Previous head SHA; present on 'update' only when commits changed",
    )


class MergeRequestWebhookPayload(BaseModel):
    """GitLab MR webhook payload (relevant fields only)."""

    model_config = ConfigDict(strict=True)

    object_kind: str = Field(description="Event type, must be 'merge_request'")
    user: WebhookUser
    project: WebhookProject
    object_attributes: MRObjectAttributes


class NoteObjectAttributes(BaseModel):
    model_config = ConfigDict(strict=True)
    note: str = Field(description="Comment body text")
    noteable_type: str = Field(description="Type of noteable: MergeRequest, Issue, etc.")


class NoteMergeRequest(BaseModel):
    model_config = ConfigDict(strict=True)
    iid: int
    title: str
    source_branch: str
    target_branch: str


class NoteWebhookPayload(BaseModel):
    """GitLab note webhook payload for MR comments."""

    model_config = ConfigDict(strict=True)
    object_kind: str
    user: WebhookUser
    project: WebhookProject
    object_attributes: NoteObjectAttributes
    merge_request: NoteMergeRequest


class PendingApproval(BaseModel):
    """Stores a pending /copilot command awaiting approval."""

    model_config = ConfigDict(strict=True)

    task_id: str = Field(description="Unique task identifier")
    requester_id: int = Field(description="User ID of the requester who can approve")
    prompt: str = Field(description="Original /copilot prompt to execute")
    mr_iid: int = Field(description="MR number")
    project_id: int = Field(description="Project ID")
    created_at: float = Field(default_factory=time.time, description="Unix timestamp")
    timeout: int = Field(default=3600, description="Timeout in seconds")
