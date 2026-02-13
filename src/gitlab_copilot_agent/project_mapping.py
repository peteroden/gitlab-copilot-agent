"""Jira project key → GitLab project mapping."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GitLabProjectMapping(BaseModel):
    """Mapping entry for a single Jira project to its GitLab counterpart."""

    model_config = ConfigDict(strict=True)

    gitlab_project_id: int = Field(description="GitLab project ID")
    clone_url: str = Field(description="GitLab repo HTTPS clone URL")
    target_branch: str = Field(default="main", description="Default MR target branch")


class ProjectMap(BaseModel):
    """Collection of Jira→GitLab project mappings loaded from config."""

    model_config = ConfigDict(strict=True)

    mappings: dict[str, GitLabProjectMapping] = Field(
        default_factory=dict,
        description="Map of Jira project key → GitLab project config",
    )

    def get(self, jira_project_key: str) -> GitLabProjectMapping | None:
        """Look up GitLab project for a Jira project key."""
        return self.mappings.get(jira_project_key)

    def __contains__(self, key: str) -> bool:
        return key in self.mappings
