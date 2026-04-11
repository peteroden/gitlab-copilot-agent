"""GitLab provisioner — creates projects and pushes demo code via httpx."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import structlog

log = structlog.get_logger()

_PAGE_SIZE = 100


def build_client(base_url: str, token: str) -> httpx.Client:
    """Create an httpx.Client configured for the GitLab v4 API."""
    return httpx.Client(
        base_url=f"{base_url.rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": token},
        timeout=30.0,
    )


def get_project(client: httpx.Client, project_path: str) -> dict[str, Any] | None:
    """Get a project by full path. Returns None if not found."""
    encoded = quote(project_path, safe="")
    resp = client.get(f"/projects/{encoded}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def create_project(
    client: httpx.Client,
    name: str,
    namespace_id: int,
    *,
    visibility: str = "private",
    description: str = "",
) -> dict[str, Any]:
    """Create a new GitLab project. Returns the project dict."""
    resp = client.post(
        "/projects",
        json={
            "name": name,
            "namespace_id": namespace_id,
            "visibility": visibility,
            "description": description,
            "initialize_with_readme": True,
        },
    )
    resp.raise_for_status()
    project: dict[str, Any] = resp.json()
    log.info("gitlab_project_created", project=project["path_with_namespace"], id=project["id"])
    return project


def get_namespace(client: httpx.Client, group_path: str) -> dict[str, Any]:
    """Look up a GitLab group/namespace by path. Falls back to user namespace."""
    encoded = quote(group_path, safe="")
    resp = client.get(f"/groups/{encoded}")
    if resp.status_code != 404:
        resp.raise_for_status()
        return resp.json()
    # Fall back to namespace search (covers user namespaces)
    ns_resp = client.get("/namespaces", params={"search": group_path})
    ns_resp.raise_for_status()
    for ns in ns_resp.json():
        if ns["full_path"] == group_path:
            return ns
    msg = f"GitLab namespace '{group_path}' not found. Check --gitlab-group."
    raise SystemExit(msg)


def _get_repository_tree(client: httpx.Client, project_id: int, branch: str) -> set[str]:
    """Fetch all file paths in a project branch via paginated tree API."""
    paths: set[str] = set()
    page = 1
    while True:
        resp = client.get(
            f"/projects/{project_id}/repository/tree",
            params={
                "ref": branch,
                "recursive": True,
                "per_page": _PAGE_SIZE,
                "page": page,
            },
        )
        if resp.status_code == 404:
            return set()
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()
        if not items:
            break
        paths.update(item["path"] for item in items)
        page += 1
    return paths


def push_files(
    client: httpx.Client,
    project_id: int,
    branch: str,
    files: dict[str, str],
    commit_message: str,
) -> None:
    """Push files to a branch using the GitLab Commits API."""
    existing = _get_repository_tree(client, project_id, branch)

    actions = [
        {
            "action": "update" if path in existing else "create",
            "file_path": path,
            "content": content,
        }
        for path, content in sorted(files.items())
    ]
    resp = client.post(
        f"/projects/{project_id}/repository/commits",
        json={
            "branch": branch,
            "commit_message": commit_message,
            "actions": actions,
        },
    )
    resp.raise_for_status()
    log.info("gitlab_files_pushed", branch=branch, file_count=len(files))


def create_merge_request(
    client: httpx.Client,
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    *,
    files: dict[str, str] | None = None,
    commit_message: str = "Add feature changes",
) -> dict[str, Any]:
    """Create a branch with file changes and open an MR.

    If *files* is provided, commits them to *source_branch* first.
    Returns the MR dict.
    """
    # Create the branch
    resp = client.post(
        f"/projects/{project_id}/repository/branches",
        json={"branch": source_branch, "ref": target_branch},
    )
    resp.raise_for_status()
    log.info("gitlab_branch_created", branch=source_branch)

    if files:
        push_files(client, project_id, source_branch, files, commit_message)

    mr_resp = client.post(
        f"/projects/{project_id}/merge_requests",
        json={
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        },
    )
    mr_resp.raise_for_status()
    mr: dict[str, Any] = mr_resp.json()
    log.info("gitlab_mr_created", mr_iid=mr["iid"], url=mr["web_url"])
    return mr


def create_webhook(
    client: httpx.Client,
    project_id: int,
    url: str,
    secret: str,
) -> dict[str, Any]:
    """Create a merge-request webhook on the project."""
    resp = client.post(
        f"/projects/{project_id}/hooks",
        json={
            "url": url,
            "token": secret,
            "merge_requests_events": True,
            "note_events": True,
            "push_events": False,
            "enable_ssl_verification": True,
        },
    )
    resp.raise_for_status()
    hook: dict[str, Any] = resp.json()
    log.info("gitlab_webhook_created", url=url)
    return hook


def load_template(template_dir: Path) -> dict[str, str]:
    """Load all files from a template directory into a dict of path→content."""
    skip_dirs = {"__pycache__", ".git"}
    files: dict[str, str] = {}
    for file_path in sorted(template_dir.rglob("*")):
        if any(part in skip_dirs for part in file_path.parts):
            continue
        if file_path.is_file() and not file_path.name.startswith(".DS_Store"):
            relative = file_path.relative_to(template_dir).as_posix()
            files[relative] = file_path.read_text()
    return files
