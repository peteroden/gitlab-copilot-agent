"""GitLab API client for repo cloning, diff fetching, and MR metadata."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

import gitlab
import structlog
from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.discussion_models import AgentIdentity, Discussion, DiscussionNote

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
    sha: str | None = None
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


class MRDiffRef(BaseModel):
    """Git diff reference SHAs for a merge request."""

    model_config = ConfigDict(frozen=True)
    base_sha: str = Field(description="Base commit SHA")
    start_sha: str = Field(description="Start commit SHA")
    head_sha: str = Field(description="Head commit SHA")


class MRChange(BaseModel):
    """A single file change in a merge request."""

    model_config = ConfigDict(frozen=True)
    old_path: str = Field(description="Original file path")
    new_path: str = Field(description="New file path")
    diff: str = Field(description="Unified diff content")
    new_file: bool = Field(default=False, description="Whether this is a new file")
    deleted_file: bool = Field(default=False, description="Whether this file was deleted")
    renamed_file: bool = Field(default=False, description="Whether this file was renamed")


class MRDetails(BaseModel):
    """Merge request metadata and file changes."""

    model_config = ConfigDict(frozen=True)
    title: str = Field(description="MR title")
    description: str | None = Field(description="MR description")
    diff_refs: MRDiffRef = Field(description="Git diff reference SHAs")
    changes: list[MRChange] = Field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list, description="List of file changes"
    )


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
    async def list_mr_discussions(self, project_id: int, mr_iid: int) -> list[Discussion]: ...
    async def get_current_user(self) -> AgentIdentity: ...


class GitLabClient:
    def __init__(self, url: str, token: str) -> None:
        self._gl = gitlab.Gitlab(url, private_token=token)
        self._token = token

    async def get_mr_details(self, project_id: int, mr_iid: int) -> MRDetails:
        def _fetch() -> MRDetails:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            changes_data: dict[str, object] = mr.changes()  # pyright: ignore[reportAssignmentType]

            diff_refs = MRDiffRef.model_validate(changes_data["diff_refs"])

            raw_changes = changes_data.get("changes", [])
            assert isinstance(raw_changes, list)
            changes: list[MRChange] = []
            for c in raw_changes:  # pyright: ignore[reportUnknownVariableType]
                if isinstance(c, dict):
                    changes.append(MRChange.model_validate(c))

            raw_desc = changes_data.get("description")
            return MRDetails(
                title=str(changes_data.get("title", "")),
                description=str(raw_desc) if raw_desc is not None else None,
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
            return mr.iid  # pyright: ignore[reportReturnType]

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
            if updated_after is not None:
                mrs = project.mergerequests.list(
                    state=state, get_all=True, updated_after=updated_after
                )
            else:
                mrs = project.mergerequests.list(state=state, get_all=True)
            return [MRListItem.model_validate(mr.attributes) for mr in mrs]

        return await asyncio.to_thread(_list)

    async def list_mr_notes(
        self, project_id: int, mr_iid: int, created_after: str | None = None
    ) -> list[NoteListItem]:
        """List notes (comments) on a merge request."""

        def _list() -> list[NoteListItem]:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            if created_after is not None:
                notes = mr.notes.list(get_all=True, created_after=created_after)
            else:
                notes = mr.notes.list(get_all=True)
            return [NoteListItem.model_validate(n.attributes) for n in notes]

        return await asyncio.to_thread(_list)

    async def resolve_project(self, id_or_path: str | int) -> int:
        """Resolve a project ID or path to its numeric ID."""

        def _resolve() -> int:
            project = self._gl.projects.get(id_or_path)
            return project.id  # pyright: ignore[reportReturnType]

        return await asyncio.to_thread(_resolve)

    async def list_mr_discussions(self, project_id: int, mr_iid: int) -> list[Discussion]:
        """Fetch all discussions on an MR with thread structure."""

        def _fetch() -> list[Discussion]:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            raw_discussions = mr.discussions.list(get_all=True)

            discussions: list[Discussion] = []
            for raw_disc in raw_discussions:
                attrs = raw_disc.attributes
                notes: list[DiscussionNote] = []
                is_inline = False

                for raw_note in attrs.get("notes", []):
                    if raw_note.get("system", False):
                        continue  # Skip system notes

                    note_type = raw_note.get("type")
                    if note_type == "DiffNote":
                        is_inline = True

                    position: dict[str, object] | None = None
                    raw_pos = raw_note.get("position")
                    if raw_pos and isinstance(raw_pos, dict):
                        position = {
                            "new_path": raw_pos.get("new_path"),  # pyright: ignore[reportUnknownMemberType]
                            "old_path": raw_pos.get("old_path"),  # pyright: ignore[reportUnknownMemberType]
                            "new_line": raw_pos.get("new_line"),  # pyright: ignore[reportUnknownMemberType]
                            "old_line": raw_pos.get("old_line"),  # pyright: ignore[reportUnknownMemberType]
                        }

                    author = raw_note.get("author", {})
                    notes.append(
                        DiscussionNote(
                            note_id=raw_note["id"],
                            author_id=author.get("id", 0),
                            author_username=author.get("username", ""),
                            body=raw_note.get("body", ""),
                            created_at=raw_note.get("created_at", ""),
                            is_system=False,  # already filtered above
                            resolved=raw_note.get("resolved"),
                            resolvable=raw_note.get("resolvable", False),
                            position=position,
                        )
                    )

                if not notes:
                    continue  # All notes were system notes

                # Check resolution at discussion level
                raw_notes = attrs.get("notes", [])
                first_note: dict[str, object] = (
                    raw_notes[0] if raw_notes else {}  # pyright: ignore[reportUnknownMemberType]
                )
                is_resolved = bool(first_note.get("resolved", False))

                discussions.append(
                    Discussion(
                        discussion_id=attrs["id"],
                        notes=notes,
                        is_resolved=is_resolved,
                        is_inline=is_inline,
                    )
                )

            return discussions

        return await asyncio.to_thread(_fetch)

    async def get_current_user(self) -> AgentIdentity:
        """Discover the identity of the authenticated user."""

        def _fetch() -> AgentIdentity:
            self._gl.auth()
            user = self._gl.user
            assert user is not None, "GitLab auth() did not populate user"
            return AgentIdentity(
                user_id=user.id,  # pyright: ignore[reportArgumentType]
                username=user.username,  # pyright: ignore[reportArgumentType]
            )

        return await asyncio.to_thread(_fetch)
