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
            "initialize_with_readme": False,
        }
    )
    log.info("gitlab_project_created", project=project.path_with_namespace, id=project.id)
    return project


def get_namespace(gl: gitlab.Gitlab, group_path: str) -> Any:
    """Look up a GitLab group/namespace by path. Raises if not found."""
    try:
        return gl.groups.get(group_path)
    except gitlab.exceptions.GitlabGetError as exc:
        msg = f"GitLab group '{group_path}' not found. Check --gitlab-group."
        raise SystemExit(msg) from exc


def push_files(
    project: Any,
    branch: str,
    files: dict[str, str],
    commit_message: str,
) -> None:
    """Push files to a branch using the GitLab Commits API."""
    actions = [
        {"action": "create", "file_path": path, "content": content}
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
