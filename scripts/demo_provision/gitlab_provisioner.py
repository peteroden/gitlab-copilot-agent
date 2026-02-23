"""GitLab provisioner — creates projects and pushes demo code via python-gitlab."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import gitlab
import structlog

log = structlog.get_logger()


def get_project(gl: gitlab.Gitlab, project_path: str) -> Any | None:
    """Get a project by full path. Returns None if not found."""
    try:
        return gl.projects.get(project_path)
    except gitlab.exceptions.GitlabGetError:
        return None


def create_project(
    gl: gitlab.Gitlab,
    name: str,
    namespace_id: int,
    *,
    visibility: str = "private",
    description: str = "",
) -> Any:
    """Create a new GitLab project. Returns the project object."""
    project = gl.projects.create(
        {
            "name": name,
            "namespace_id": namespace_id,
            "visibility": visibility,
            "description": description,
            "initialize_with_readme": True,
        }
    )
    log.info("gitlab_project_created", project=project.path_with_namespace, id=project.id)
    return project


def get_namespace(gl: gitlab.Gitlab, group_path: str) -> Any:
    """Look up a GitLab group/namespace by path. Falls back to user namespace."""
    try:
        return gl.groups.get(group_path)
    except gitlab.exceptions.GitlabGetError:
        pass
    # Fall back to namespace search (covers user namespaces)
    namespaces = gl.namespaces.list(search=group_path)
    for ns in namespaces:
        if ns.full_path == group_path:
            return ns
    msg = f"GitLab namespace '{group_path}' not found. Check --gitlab-group."
    raise SystemExit(msg)


def push_files(
    project: Any,
    branch: str,
    files: dict[str, str],
    commit_message: str,
) -> None:
    """Push files to a branch using the GitLab Commits API."""
    # For projects initialized with README, that file already exists on the branch.
    # Use "update" for README.md and "create" for everything else.
    existing = set()
    try:
        tree = project.repository_tree(ref=branch, recursive=True, get_all=True)
        existing = {item["path"] for item in tree}
    except Exception:
        pass

    actions = [
        {
            "action": "update" if path in existing else "create",
            "file_path": path,
            "content": content,
        }
        for path, content in sorted(files.items())
    ]
    project.commits.create(
        {
            "branch": branch,
            "commit_message": commit_message,
            "actions": actions,
        }
    )
    log.info("gitlab_files_pushed", branch=branch, file_count=len(files))


def create_merge_request(
    project: Any,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    *,
    files: dict[str, str] | None = None,
    commit_message: str = "Add feature changes",
) -> Any:
    """Create a branch with file changes and open an MR.

    If *files* is provided, commits them to *source_branch* first.
    Returns the MR object.
    """
    # Create the branch
    project.branches.create({"branch": source_branch, "ref": target_branch})
    log.info("gitlab_branch_created", branch=source_branch)

    if files:
        push_files(project, source_branch, files, commit_message)

    mr = project.mergerequests.create(
        {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
    )
    log.info("gitlab_mr_created", mr_iid=mr.iid, url=mr.web_url)
    return mr


def create_webhook(
    project: Any,
    url: str,
    secret: str,
) -> Any:
    """Create a merge-request webhook on the project."""
    hook = project.hooks.create(
        {
            "url": url,
            "token": secret,
            "merge_requests_events": True,
            "note_events": True,
            "push_events": False,
            "enable_ssl_verification": True,
        }
    )
    log.info("gitlab_webhook_created", url=url)
    return hook


def load_template(template_dir: Path) -> dict[str, str]:
    """Load all files from a template directory into a dict of path→content."""
    files: dict[str, str] = {}
    for file_path in sorted(template_dir.rglob("*")):
        if file_path.is_file() and not file_path.name.startswith(".DS_Store"):
            relative = file_path.relative_to(template_dir).as_posix()
            files[relative] = file_path.read_text()
    return files
