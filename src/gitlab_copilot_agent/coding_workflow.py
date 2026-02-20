"""Shared coding workflow: execute task → apply patch (if k8s) → commit → push."""

from __future__ import annotations

from pathlib import Path

import structlog

from gitlab_copilot_agent.git_operations import git_apply_patch, git_head_sha
from gitlab_copilot_agent.task_executor import CodingResult, TaskResult

log = structlog.get_logger()


async def apply_coding_result(result: TaskResult, repo_path: Path) -> None:
    """Apply a CodingResult patch to the local clone if present.

    For LocalTaskExecutor results the patch is empty — files are already on disk.
    For KubernetesTaskExecutor results the patch is applied via ``git apply --3way``.

    Raises:
        RuntimeError: If base_sha doesn't match the local HEAD (clone diverged).
        ValueError: If patch contains path traversal (checked inside git_apply_patch).
    """
    if not isinstance(result, CodingResult) or not result.patch:
        return

    local_head = await git_head_sha(repo_path)
    if result.base_sha and result.base_sha != local_head:
        raise RuntimeError(
            f"Clone diverged: pod base_sha={result.base_sha[:12]} vs local HEAD={local_head[:12]}"
        )
    await git_apply_patch(repo_path, result.patch)
    await log.ainfo("coding_patch_applied", repo=str(repo_path))
