"""Pydantic models for GitLab webhook payloads."""

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
