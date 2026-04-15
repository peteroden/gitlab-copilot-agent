"""Git operations — clone, commit, push, branch, patch, archive."""

from gitlab_copilot_agent.git.archive import extract_repo_tarball, tar_repo_to_bytes
from gitlab_copilot_agent.git.clone import CLONE_DIR_PREFIX, TransientCloneError, git_clone
from gitlab_copilot_agent.git.operations import (
    git_commit,
    git_create_branch,
    git_diff_staged,
    git_head_sha,
    git_push,
    git_unique_branch,
)
from gitlab_copilot_agent.git.patches import MAX_PATCH_SIZE, git_apply_patch
from gitlab_copilot_agent.git.validation import validate_clone_url_host

__all__ = [
    "CLONE_DIR_PREFIX",
    "MAX_PATCH_SIZE",
    "TransientCloneError",
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
