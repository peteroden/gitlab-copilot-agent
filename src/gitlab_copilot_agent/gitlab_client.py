"""GitLab API client for repo cloning, diff fetching, and MR metadata."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import gitlab
import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger()


class MRAuthor(BaseModel):
    """MR author from GitLab API list response."""

    model_config = ConfigDict(extra="ignore")
    id: int
    username: str


class MRListItem(BaseModel):
    """Subset of fields from GitLab MR list API response."""

    model_config = ConfigDict(extra="ignore")
    iid: int
    title: str
    description: str | None = None
    source_branch: str
    target_branch: str
    sha: str
    web_url: str
    state: str
    author: MRAuthor
    updated_at: str


class NoteListItem(BaseModel):
    """Subset of fields from GitLab MR notes API response."""

    model_config = ConfigDict(extra="ignore")
    id: int
    body: str
    author: MRAuthor
    system: bool = False
    created_at: str


@dataclass(frozen=True)
class MRDiffRef:
    base_sha: str
    start_sha: str
    head_sha: str


@dataclass(frozen=True)
class MRChange:
    old_path: str
    new_path: str
    diff: str
    new_file: bool = False
    deleted_file: bool = False
    renamed_file: bool = False


@dataclass(frozen=True)
class MRDetails:
    title: str
    description: str | None
    diff_refs: MRDiffRef
    changes: list[MRChange] = field(default_factory=list)


class GitLabClientProtocol(Protocol):
    async def get_mr_details(self, project_id: int, mr_iid: int) -> MRDetails: ...
    async def clone_repo(self, clone_url: str, branch: str, token: str) -> Path: ...
    async def cleanup(self, repo_path: Path) -> None: ...
    async def create_merge_request(
        self, project_id: int, source_branch: str, target_branch: str, title: str, description: str
    ) -> int: ...
    async def post_mr_comment(self, project_id: int, mr_iid: int, body: str) -> None: ...
    async def list_project_mrs(
        self, project_id: int, state: str = "opened", updated_after: str | None = None
    ) -> list[MRListItem]: ...
    async def list_mr_notes(
        self, project_id: int, mr_iid: int, created_after: str | None = None
    ) -> list[NoteListItem]: ...
    async def resolve_project(self, id_or_path: str | int) -> int: ...


class GitLabClient:
    def __init__(self, url: str, token: str) -> None:
        self._gl = gitlab.Gitlab(url, private_token=token)
        self._token = token

    async def get_mr_details(self, project_id: int, mr_iid: int) -> MRDetails:
        def _fetch() -> MRDetails:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            changes_data = mr.changes()

            diff_refs_raw = changes_data["diff_refs"]
            diff_refs = MRDiffRef(
                base_sha=diff_refs_raw["base_sha"],
                start_sha=diff_refs_raw["start_sha"],
                head_sha=diff_refs_raw["head_sha"],
            )

            changes = [
                MRChange(
                    old_path=c["old_path"],
                    new_path=c["new_path"],
                    diff=c["diff"],
                    new_file=c.get("new_file", False),
                    deleted_file=c.get("deleted_file", False),
                    renamed_file=c.get("renamed_file", False),
                )
                for c in changes_data.get("changes", [])
            ]

            return MRDetails(
                title=changes_data.get("title", ""),
                description=changes_data.get("description"),
                diff_refs=diff_refs,
                changes=changes,
            )

        return await asyncio.to_thread(_fetch)

    async def clone_repo(
        self, clone_url: str, branch: str, token: str, *, clone_dir: str | None = None
    ) -> Path:
        from gitlab_copilot_agent.git_operations import git_clone

        return await git_clone(clone_url, branch, token, clone_dir=clone_dir)

    async def cleanup(self, repo_path: Path) -> None:
        import shutil

        await asyncio.to_thread(shutil.rmtree, repo_path, True)
        await log.ainfo("repo_cleaned", path=str(repo_path))

    async def create_merge_request(
        self,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> int:
        """Create a merge request. Returns the MR IID."""

        def _create() -> int:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.create(
                {
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "title": title,
                    "description": description,
                }
            )
            return mr.iid  # type: ignore[no-any-return]

        return await asyncio.to_thread(_create)

    async def post_mr_comment(self, project_id: int, mr_iid: int, body: str) -> None:
        """Post a comment on a merge request."""

        def _post() -> None:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            mr.notes.create({"body": body})

        await asyncio.to_thread(_post)

    async def list_project_mrs(
        self, project_id: int, state: str = "opened", updated_after: str | None = None
    ) -> list[MRListItem]:
        """List merge requests for a project."""

        def _list() -> list[MRListItem]:
            project = self._gl.projects.get(project_id)
            kwargs: dict[str, object] = {"state": state, "get_all": True}
            if updated_after is not None:
                kwargs["updated_after"] = updated_after
            mrs = project.mergerequests.list(**kwargs)
            return [MRListItem.model_validate(mr.attributes) for mr in mrs]

        return await asyncio.to_thread(_list)

    async def list_mr_notes(
        self, project_id: int, mr_iid: int, created_after: str | None = None
    ) -> list[NoteListItem]:
        """List notes (comments) on a merge request."""

        def _list() -> list[NoteListItem]:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            kwargs: dict[str, object] = {"get_all": True}
            if created_after is not None:
                kwargs["created_after"] = created_after
            notes = mr.notes.list(**kwargs)
            return [NoteListItem.model_validate(n.attributes) for n in notes]

        return await asyncio.to_thread(_list)

    async def resolve_project(self, id_or_path: str | int) -> int:
        """Resolve a project ID or path to its numeric ID."""

        def _resolve() -> int:
            project = self._gl.projects.get(id_or_path)
            return project.id  # type: ignore[no-any-return]

        return await asyncio.to_thread(_resolve)
