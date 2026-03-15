"""Project registry — resolves Jira→GitLab project context at startup."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from gitlab_copilot_agent.credential_registry import CredentialRegistry
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.mapping_models import RenderedMap

log = structlog.get_logger()


@dataclass(frozen=True, repr=False)
class ResolvedProject:
    """Fully resolved project context ready for runtime use."""

    jira_project: str
    repo: str
    gitlab_project_id: int
    clone_url: str
    target_branch: str
    credential_ref: str
    token: str

    def __repr__(self) -> str:
        return (
            f"ResolvedProject(jira_project={self.jira_project!r}, "
            f"repo={self.repo!r}, gitlab_project_id={self.gitlab_project_id}, "
            f"credential_ref={self.credential_ref!r}, token='***')"
        )


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
                )
            )
        registry = cls(projects)
        await log.ainfo("project_registry_loaded", count=len(projects))
        return registry
