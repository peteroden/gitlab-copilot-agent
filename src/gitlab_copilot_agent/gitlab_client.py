"""GitLab API client — async httpx-based, fully typed.

Replaces the previous python-gitlab wrapper.  All API calls are natively
async via httpx — no ``asyncio.to_thread`` overhead.  Transient failures
(HTTP 429, 5xx, transport errors) are retried with exponential backoff
for idempotent (GET) requests only.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, Protocol  # Any: unvalidated JSON from resp.json()
from urllib.parse import quote

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from gitlab_copilot_agent.discussion_models import AgentIdentity, Discussion, DiscussionNote

log = structlog.get_logger()

_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.0
_DIFF_REFS_MAX_RETRIES = 5
_PAGE_SIZE = 100


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


class MRCommit(BaseModel):
    """A single commit on a merge request."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    id: str = Field(description="Full commit SHA")
    title: str = Field(description="First line of the commit message")
    message: str = Field(description="Full commit message body")


class GitLabClientProtocol(Protocol):
    """Protocol for GitLab API operations used throughout the codebase."""

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
    async def resolve_discussion(
        self, project_id: int, mr_iid: int, discussion_id: str
    ) -> None: ...
    async def reply_to_discussion(
        self, project_id: int, mr_iid: int, discussion_id: str, body: str
    ) -> None: ...
    async def compare_commits(
        self, project_id: int, from_sha: str, to_sha: str
    ) -> list[MRChange]: ...
    async def get_mr_commits(self, project_id: int, mr_iid: int) -> list[MRCommit]: ...
    async def create_mr_discussion(
        self, project_id: int, mr_iid: int, body: str, position: dict[str, object]
    ) -> None: ...


class GitLabClient:
    """Async GitLab REST API client using httpx.

    All API calls use async httpx directly — no python-gitlab dependency.
    Retries transient failures (429, 5xx) with exponential backoff for
    idempotent GET requests only.  Mutating calls (POST/PUT) are *not*
    retried on server errors to avoid duplicate side-effects.

    Args:
        url: GitLab instance base URL (e.g. ``https://gitlab.example.com``).
        token: GitLab private access token.
    """

    def __init__(self, url: str, token: str) -> None:
        self._base_url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{self._base_url}/api/v4",
            headers={"PRIVATE-TOKEN": token},
            timeout=_DEFAULT_TIMEOUT,
        )
        self._token = token

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> GitLabClient:
        """Enter async context manager."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Exit async context manager and close the HTTP client."""
        await self.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        idempotent: bool | None = None,
        json: object | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Make an API request, retrying transient errors for safe methods.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            path: API path relative to ``/api/v4``.
            idempotent: Override retry eligibility.  Defaults to ``True``
                for GET/HEAD, ``False`` otherwise.
            json: JSON request body (for POST/PUT).
            params: Query parameters.

        Returns:
            The successful ``httpx.Response``.

        Raises:
            httpx.HTTPStatusError: On non-retryable HTTP errors.
            httpx.TransportError: When all retries are exhausted.
        """
        can_retry = idempotent if idempotent is not None else method.upper() in ("GET", "HEAD")
        max_attempts = _MAX_RETRIES if can_retry else 1
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            try:
                resp = await self._client.request(method, path, json=json, params=params)
                if (
                    resp.status_code == 429 or resp.status_code >= 500
                ) and attempt < max_attempts - 1:
                    delay = _retry_delay(resp, attempt)
                    log.warning(
                        "gitlab_api_retry",
                        status=resp.status_code,
                        attempt=attempt,
                        delay=delay,
                        path=path,
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < max_attempts - 1:
                    delay = _RETRY_BACKOFF * (2**attempt)
                    log.warning(
                        "gitlab_transport_retry",
                        attempt=attempt,
                        delay=delay,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Request failed after retries")  # pragma: no cover

    async def _paginate(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages of a paginated GitLab API endpoint.

        Args:
            path: API path relative to ``/api/v4``.
            params: Query parameters (``per_page`` is added automatically).

        Returns:
            Concatenated list of JSON objects from all pages.
        """
        all_items: list[dict[str, Any]] = []
        page_params: dict[str, str] = {**(params or {}), "per_page": str(_PAGE_SIZE)}
        page = 1

        while True:
            page_params["page"] = str(page)
            resp = await self._request("GET", path, params=page_params)
            items: list[dict[str, Any]] = resp.json()
            if not items:
                break
            all_items.extend(items)
            if len(items) < _PAGE_SIZE:
                break
            page += 1

        return all_items

    # ------------------------------------------------------------------
    # Project
    # ------------------------------------------------------------------

    async def resolve_project(self, id_or_path: str | int) -> int:
        """Resolve a project path or ID to its numeric ID.

        Args:
            id_or_path: Numeric project ID or ``"group/project"`` path.

        Returns:
            The numeric project ID.
        """
        if isinstance(id_or_path, int):
            return id_or_path
        encoded = quote(str(id_or_path), safe="")
        resp = await self._request("GET", f"/projects/{encoded}")
        data: dict[str, Any] = resp.json()
        return int(data["id"])

    # ------------------------------------------------------------------
    # Merge requests
    # ------------------------------------------------------------------

    async def get_mr_details(self, project_id: int, mr_iid: int) -> MRDetails:
        """Fetch MR metadata and file changes.

        Retries when ``diff_refs`` is null — a known GitLab race condition
        on freshly-created MRs where the diff is still being computed.

        Args:
            project_id: Numeric project ID.
            mr_iid: Merge request internal ID.

        Returns:
            Parsed MR details with diff refs and file changes.
        """
        data: dict[str, Any] = {}
        for attempt in range(_DIFF_REFS_MAX_RETRIES):
            resp = await self._request(
                "GET",
                f"/projects/{project_id}/merge_requests/{mr_iid}/changes",
            )
            data = resp.json()
            if data.get("diff_refs") is not None:
                break
            if attempt < _DIFF_REFS_MAX_RETRIES - 1:
                await asyncio.sleep(min(2**attempt, 8))

        if data.get("diff_refs") is None:
            msg = (
                f"diff_refs is null for MR !{mr_iid} in project {project_id} "
                f"after retries — GitLab may still be computing the diff"
            )
            raise RuntimeError(msg)

        diff_refs = MRDiffRef.model_validate(data["diff_refs"])
        changes = [
            MRChange.model_validate(c) for c in data.get("changes", []) if isinstance(c, dict)
        ]
        raw_desc = data.get("description")
        return MRDetails(
            title=str(data.get("title", "")),
            description=str(raw_desc) if raw_desc is not None else None,
            diff_refs=diff_refs,
            changes=changes,
        )

    async def get_mr_commits(self, project_id: int, mr_iid: int) -> list[MRCommit]:
        """Fetch commits on a merge request for developer intent context.

        Args:
            project_id: Numeric project ID.
            mr_iid: Merge request internal ID.

        Returns:
            List of commits on the MR.
        """
        items = await self._paginate(
            f"/projects/{project_id}/merge_requests/{mr_iid}/commits",
        )
        return [MRCommit.model_validate(c) for c in items]

    async def list_project_mrs(
        self,
        project_id: int,
        state: str = "opened",
        updated_after: str | None = None,
    ) -> list[MRListItem]:
        """List merge requests for a project.

        Args:
            project_id: Numeric project ID.
            state: MR state filter (``opened``, ``merged``, ``closed``, ``all``).
            updated_after: ISO 8601 timestamp to filter by last update.

        Returns:
            List of matching merge requests.
        """
        params: dict[str, str] = {"state": state}
        if updated_after is not None:
            params["updated_after"] = updated_after
        items = await self._paginate(
            f"/projects/{project_id}/merge_requests",
            params=params,
        )
        return [MRListItem.model_validate(mr) for mr in items]

    async def list_mr_notes(
        self,
        project_id: int,
        mr_iid: int,
        created_after: str | None = None,
    ) -> list[NoteListItem]:
        """List notes (comments) on a merge request.

        Args:
            project_id: Numeric project ID.
            mr_iid: Merge request internal ID.
            created_after: ISO 8601 timestamp to filter notes.

        Returns:
            List of notes on the MR.
        """
        params: dict[str, str] = {}
        if created_after is not None:
            params["created_after"] = created_after
        items = await self._paginate(
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
            params=params,
        )
        return [NoteListItem.model_validate(n) for n in items]

    async def create_merge_request(
        self,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> int:
        """Create a merge request. Returns the MR IID.

        Args:
            project_id: Numeric project ID.
            source_branch: Source branch name.
            target_branch: Target branch name.
            title: MR title.
            description: MR description.

        Returns:
            The IID of the newly created merge request.
        """
        resp = await self._request(
            "POST",
            f"/projects/{project_id}/merge_requests",
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            },
        )
        data: dict[str, Any] = resp.json()
        return int(data["iid"])

    async def post_mr_comment(self, project_id: int, mr_iid: int, body: str) -> None:
        """Post a note (comment) on a merge request.

        Args:
            project_id: Numeric project ID.
            mr_iid: Merge request internal ID.
            body: Comment body text.
        """
        await self._request(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
            json={"body": body},
        )

    async def create_mr_discussion(
        self,
        project_id: int,
        mr_iid: int,
        body: str,
        position: dict[str, object],
    ) -> None:
        """Create an inline discussion on a merge request diff.

        Args:
            project_id: Numeric project ID.
            mr_iid: Merge request internal ID.
            body: Discussion body text.
            position: Diff position dict with ``base_sha``, ``start_sha``,
                ``head_sha``, ``position_type``, ``old_path``, ``new_path``,
                ``new_line``.
        """
        await self._request(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions",
            json={"body": body, "position": position},
        )

    # ------------------------------------------------------------------
    # Discussions
    # ------------------------------------------------------------------

    async def list_mr_discussions(self, project_id: int, mr_iid: int) -> list[Discussion]:
        """Fetch all discussions on an MR with thread structure.

        Filters out system notes and extracts inline/position metadata.

        Args:
            project_id: Numeric project ID.
            mr_iid: Merge request internal ID.

        Returns:
            Parsed discussion threads.
        """
        raw_discussions = await self._paginate(
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions",
        )
        return _parse_discussions(raw_discussions)

    async def resolve_discussion(self, project_id: int, mr_iid: int, discussion_id: str) -> None:
        """Resolve a discussion thread via the GitLab API.

        Args:
            project_id: Numeric project ID.
            mr_iid: Merge request internal ID.
            discussion_id: GitLab discussion ID.
        """
        await self._request(
            "PUT",
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}",
            json={"resolved": True},
        )

    async def reply_to_discussion(
        self, project_id: int, mr_iid: int, discussion_id: str, body: str
    ) -> None:
        """Post a reply to an existing discussion thread.

        Args:
            project_id: Numeric project ID.
            mr_iid: Merge request internal ID.
            discussion_id: GitLab discussion ID.
            body: Reply body text.
        """
        await self._request(
            "POST",
            (f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}/notes"),
            json={"body": body},
        )

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------

    async def get_current_user(self) -> AgentIdentity:
        """Discover the identity of the authenticated user.

        Returns:
            The agent's GitLab user ID and username.
        """
        resp = await self._request("GET", "/user")
        data: dict[str, Any] = resp.json()
        return AgentIdentity(user_id=int(data["id"]), username=str(data["username"]))

    # ------------------------------------------------------------------
    # Repository
    # ------------------------------------------------------------------

    async def compare_commits(self, project_id: int, from_sha: str, to_sha: str) -> list[MRChange]:
        """Compare two commits via the Repository Compare API.

        Returns file changes between *from_sha* and *to_sha* in the same
        shape as MR changes, enabling reuse of existing diff assembly.

        Args:
            project_id: Numeric project ID.
            from_sha: Base commit SHA.
            to_sha: Head commit SHA.

        Returns:
            List of file changes between the two commits.
        """
        resp = await self._request(
            "GET",
            f"/projects/{project_id}/repository/compare",
            params={"from": from_sha, "to": to_sha},
        )
        data: dict[str, Any] = resp.json()
        raw_diffs: Any = data.get("diffs", [])  # type narrowed by isinstance below
        if not isinstance(raw_diffs, list):
            return []
        return [
            MRChange.model_validate(d)
            for d in raw_diffs  # pyright: ignore[reportUnknownVariableType]
            if isinstance(d, dict)
        ]

    # ------------------------------------------------------------------
    # Git operations (unchanged — delegates to git_operations module)
    # ------------------------------------------------------------------

    async def clone_repo(
        self, clone_url: str, branch: str, token: str, *, clone_dir: str | None = None
    ) -> Path:
        """Clone a repository via git subprocess.

        Args:
            clone_url: Git HTTP clone URL.
            branch: Branch to check out.
            token: Token for git authentication.
            clone_dir: Optional directory for the clone.

        Returns:
            Path to the cloned repository.
        """
        from gitlab_copilot_agent.git import git_clone

        return await git_clone(clone_url, branch, token, clone_dir=clone_dir)

    async def cleanup(self, repo_path: Path) -> None:
        """Remove a cloned repository from disk.

        Args:
            repo_path: Path to the cloned repository.
        """
        await asyncio.to_thread(shutil.rmtree, repo_path, True)
        await log.ainfo("repo_cleaned", path=str(repo_path))


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Compute retry delay, respecting ``Retry-After`` header if present."""
    retry_after = resp.headers.get("retry-after")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return _RETRY_BACKOFF * (2**attempt)


def _parse_note(raw_note: dict[str, Any]) -> DiscussionNote:
    """Parse a single raw note dict into a DiscussionNote model."""
    position: dict[str, object] | None = None
    raw_pos: dict[str, Any] | None = raw_note.get("position")
    if raw_pos is not None:
        position = {
            "new_path": raw_pos.get("new_path"),
            "old_path": raw_pos.get("old_path"),
            "new_line": raw_pos.get("new_line"),
            "old_line": raw_pos.get("old_line"),
            "head_sha": raw_pos.get("head_sha"),
        }

    author: dict[str, Any] = raw_note.get("author", {})
    raw_resolved_by: dict[str, Any] | None = raw_note.get("resolved_by")
    resolved_by_id: int | None = None
    if isinstance(raw_resolved_by, dict):
        rbid = raw_resolved_by.get("id")
        if isinstance(rbid, int):
            resolved_by_id = rbid

    return DiscussionNote(
        note_id=raw_note["id"],
        author_id=author.get("id", 0),
        author_username=author.get("username", ""),
        body=raw_note.get("body", ""),
        created_at=raw_note.get("created_at", ""),
        is_system=False,
        resolved=raw_note.get("resolved"),
        resolved_by_id=resolved_by_id,
        resolvable=raw_note.get("resolvable", False),
        position=position,
    )


def _parse_discussions(raw_discussions: list[dict[str, Any]]) -> list[Discussion]:
    """Parse raw discussion dicts from the GitLab API into Discussion models."""
    discussions: list[Discussion] = []

    for raw_disc in raw_discussions:
        notes: list[DiscussionNote] = []
        is_inline = False

        for raw_note in raw_disc.get("notes", []):
            if raw_note.get("system", False):
                continue
            if raw_note.get("type") == "DiffNote":
                is_inline = True
            notes.append(_parse_note(raw_note))

        if not notes:
            continue

        raw_notes: list[dict[str, Any]] = raw_disc.get("notes", [])
        first_note = raw_notes[0] if raw_notes else {}
        is_resolved = bool(first_note.get("resolved", False))

        discussions.append(
            Discussion(
                discussion_id=raw_disc["id"],
                notes=notes,
                is_resolved=is_resolved,
                is_inline=is_inline,
            )
        )

    return discussions
