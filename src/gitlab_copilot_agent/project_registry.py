"""Project registry — resolves Jira→GitLab project context at startup."""

from __future__ import annotations

import structlog
from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.credential_registry import CredentialRegistry
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.mapping_models import RenderedMap

log = structlog.get_logger()


class ResolvedProject(BaseModel):
    """Fully resolved project context ready for runtime use."""

    model_config = ConfigDict(frozen=True)

    jira_project: str = Field(description="Jira project key")
    repo: str = Field(description="GitLab repo path_with_namespace")
    gitlab_project_id: int = Field(description="Numeric GitLab project ID")
    clone_url: str = Field(description="Git HTTP clone URL")
    target_branch: str = Field(description="Default target branch")
    credential_ref: str = Field(description="Credential registry key")
    token: str = Field(description="Resolved GitLab token", repr=False)
    plugins: list[str] = Field(default_factory=list, description="Copilot CLI plugin specs")


class ProjectRegistry:
    """Immutable lookup of Jira key or GitLab project ID → ResolvedProject."""

    def __init__(self, projects: list[ResolvedProject]) -> None:
        self._by_jira = {p.jira_project: p for p in projects}
        self._by_project_id: dict[int, ResolvedProject] = {}
        for p in projects:
            if p.gitlab_project_id in self._by_project_id:
                other = self._by_project_id[p.gitlab_project_id]
                raise ValueError(
                    f"Duplicate gitlab_project_id {p.gitlab_project_id}: "
                    f"{other.jira_project} and {p.jira_project}"
                )
            self._by_project_id[p.gitlab_project_id] = p

    def get_by_jira(self, jira_key: str) -> ResolvedProject | None:
        return self._by_jira.get(jira_key)

    def get_by_project_id(self, project_id: int) -> ResolvedProject | None:
        return self._by_project_id.get(project_id)

    def jira_keys(self) -> set[str]:
        return set(self._by_jira)

    @classmethod
    async def from_rendered_map(
        cls,
        rendered: RenderedMap,
        credentials: CredentialRegistry,
        gitlab_url: str,
    ) -> ProjectRegistry:
        projects: list[ResolvedProject] = []
        base_url = gitlab_url.rstrip("/")
        for jira_key, binding in rendered.mappings.items():
            token = credentials.resolve(binding.credential_ref)
            client = GitLabClient(gitlab_url, token)
            pid = await client.resolve_project(binding.repo)
            projects.append(
                ResolvedProject(
                    jira_project=jira_key,
                    repo=binding.repo,
                    gitlab_project_id=pid,
                    clone_url=f"{base_url}/{binding.repo}.git",
                    target_branch=binding.target_branch,
                    credential_ref=binding.credential_ref,
                    token=token,
                    plugins=binding.plugins,
                )
            )
        registry = cls(projects)
        await log.ainfo("project_registry_loaded", count=len(projects))
        return registry
