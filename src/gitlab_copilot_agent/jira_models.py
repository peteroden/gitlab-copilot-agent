"""Pydantic models for Jira REST API responses."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JiraUser(BaseModel):
    """Jira user reference."""

    model_config = ConfigDict(strict=True)

    account_id: str = Field(description="Jira Cloud account ID")
    display_name: str = Field(description="User display name")
    email_address: str | None = Field(default=None, description="User email if available")


class JiraStatus(BaseModel):
    """Jira issue status."""

    model_config = ConfigDict(strict=True)

    name: str = Field(description="Status display name, e.g. 'AI Ready'")
    id: str = Field(description="Status ID")


class JiraIssueFields(BaseModel):
    """Fields within a Jira issue response."""

    model_config = ConfigDict(strict=True)

    summary: str = Field(description="Issue title/summary")
    description: str | None = Field(default=None, description="Issue description (ADF or text)")
    status: JiraStatus = Field(description="Current issue status")
    assignee: JiraUser | None = Field(default=None, description="Assigned user")
    labels: list[str] = Field(default_factory=list, description="Issue labels")


class JiraIssue(BaseModel):
    """A Jira issue from the REST API."""

    model_config = ConfigDict(strict=True)

    id: str = Field(description="Jira issue ID")
    key: str = Field(description="Issue key, e.g. 'PROJ-123'")
    fields: JiraIssueFields = Field(description="Issue fields")

    @property
    def project_key(self) -> str:
        """Extract project key from issue key (e.g. 'PROJ' from 'PROJ-123')."""
        return self.key.rsplit("-", maxsplit=1)[0]


class JiraSearchResponse(BaseModel):
    """Response from Jira v3 search/jql endpoint."""

    model_config = ConfigDict(strict=True)

    issues: list[JiraIssue] = Field(default_factory=list, description="Matching issues")
    next_page_token: str | None = Field(
        default=None, alias="nextPageToken", description="Token for next page"
    )
    total: int = Field(default=0, description="Total matching issues")


class JiraTransition(BaseModel):
    """A Jira issue transition."""

    model_config = ConfigDict(strict=True)

    id: str = Field(description="Transition ID")
    name: str = Field(description="Transition name")


class JiraTransitionsResponse(BaseModel):
    """Response from Jira transitions endpoint."""

    model_config = ConfigDict(strict=True)

    transitions: list[JiraTransition] = Field(
        default_factory=list, description="Available transitions"
    )
