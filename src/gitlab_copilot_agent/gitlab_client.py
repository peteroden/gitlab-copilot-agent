"""GitLab API client for repo cloning, diff fetching, and MR metadata."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
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
        self, clone_url: str, branch: str, token: str
    ) -> Path:
        tmp_dir = Path(tempfile.mkdtemp(prefix="mr-review-"))
        auth_url = clone_url.replace("https://", f"https://oauth2:{token}@")

        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth=1", "--branch", branch, auth_url, str(tmp_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            msg = f"git clone failed: {stderr.decode().strip()}"
            raise RuntimeError(msg)

        await log.ainfo("repo_cloned", path=str(tmp_dir), branch=branch)
        return tmp_dir

    async def cleanup(self, repo_path: Path) -> None:
        await asyncio.to_thread(shutil.rmtree, repo_path, True)
        await log.ainfo("repo_cleaned", path=str(repo_path))
