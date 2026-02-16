"""GitLab API client for repo cloning, diff fetching, and MR metadata."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import gitlab
import structlog

log = structlog.get_logger()


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
