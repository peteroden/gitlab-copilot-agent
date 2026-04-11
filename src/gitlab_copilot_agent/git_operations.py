"""Backward-compatible re-export shim — use ``gitlab_copilot_agent.git`` instead."""

# ruff: noqa: F401
from gitlab_copilot_agent.git.archive import extract_repo_tarball, tar_repo_to_bytes
from gitlab_copilot_agent.git.clone import CLONE_DIR_PREFIX, TransientCloneError, git_clone
from gitlab_copilot_agent.git.operations import (
    _run_git,  # pyright: ignore[reportPrivateUsage] — re-export for test compat
    git_commit,
    git_create_branch,
    git_diff_staged,
    git_head_sha,
    git_push,
    git_unique_branch,
)
from gitlab_copilot_agent.git.patches import (
    MAX_PATCH_SIZE,
    _validate_patch,  # pyright: ignore[reportPrivateUsage]
    git_apply_patch,
)
from gitlab_copilot_agent.git.validation import (
    is_transient_clone_error as _is_transient_clone_error,
)
from gitlab_copilot_agent.git.validation import (
    sanitize_url_for_log as _sanitize_url_for_log,
)
from gitlab_copilot_agent.git.validation import (
    validate_clone_url as _validate_clone_url,
)
from gitlab_copilot_agent.git.validation import (
    validate_clone_url_host,
)

__all__ = [
    "CLONE_DIR_PREFIX",
    "MAX_PATCH_SIZE",
    "TransientCloneError",
    "_is_transient_clone_error",
    "_run_git",
    "_sanitize_url_for_log",
    "_validate_clone_url",
    "_validate_patch",
    "extract_repo_tarball",
    "git_apply_patch",
    "git_clone",
    "git_commit",
    "git_create_branch",
    "git_diff_staged",
    "git_head_sha",
    "git_push",
    "git_unique_branch",
    "tar_repo_to_bytes",
    "validate_clone_url_host",
]
