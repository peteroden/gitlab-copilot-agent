"""SDK-native sandboxing for Copilot sessions via pre-tool-use hooks."""

from __future__ import annotations

import os
from collections.abc import Callable

from copilot.types import (
    PermissionRequest,
    PermissionRequestResult,
    PreToolUseHookInput,
    PreToolUseHookOutput,
)

# Tools that operate on file paths
_FILE_TOOLS = frozenset({"read", "write", "edit", "create"})


def _is_path_inside_repo(path: str, repo_path: str) -> bool:
    """Check if a path resolves to within the repo directory.

    Args:
        path: The path to check (absolute or relative)
        repo_path: The allowed repository directory

    Returns:
        True if the resolved path is inside repo_path, False otherwise
    """
    # Resolve both paths to handle symlinks and relative paths
    try:
        # If path is relative, join it with repo_path first
        if not os.path.isabs(path):
            path = os.path.join(repo_path, path)

        resolved_path = os.path.realpath(path)
        resolved_repo = os.path.realpath(repo_path)

        # Ensure repo path ends with separator for accurate startswith check
        if not resolved_repo.endswith(os.sep):
            resolved_repo += os.sep

        return resolved_path.startswith(resolved_repo)
    except (OSError, ValueError):
        # If path resolution fails, deny access
        return False


def create_pre_tool_use_hook(
    repo_path: str,
) -> Callable[
    [PreToolUseHookInput, dict[str, str]],
    PreToolUseHookOutput | None,
]:
    """Create a pre-tool-use hook that restricts operations to repo_path.

    File read/write operations are only allowed within the repo directory.
    Shell operations are always allowed (the agent needs them for git, tests, etc.).
    URL operations are always allowed (the agent needs GitHub API access).

    Args:
        repo_path: The repository directory path to restrict operations to

    Returns:
        A hook function that validates tool operations
    """

    def pre_tool_use_hook(
        hook_input: PreToolUseHookInput,
        env: dict[str, str],  # noqa: ARG001
    ) -> PreToolUseHookOutput | None:
        """Validate tool operation against allowed repo path.

        Args:
            hook_input: Tool use information including name and args
            env: Environment variables (unused)

        Returns:
            PreToolUseHookOutput with deny decision if outside repo, None to allow
        """
        tool_name = hook_input.get("toolName", "")
        tool_args = hook_input.get("toolArgs")

        # Only check file operation tools
        if tool_name not in _FILE_TOOLS:
            return None

        # Handle missing or invalid toolArgs
        if not isinstance(tool_args, dict):
            return None

        # Extract the path from toolArgs
        # Different tools use different field names
        path = tool_args.get("path") or tool_args.get("file_path")

        if not path or not isinstance(path, str):
            return None

        # Check if path is within repo
        if not _is_path_inside_repo(path, repo_path):
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": "Path outside allowed repository directory",
            }

        # Path is inside repo, allow the operation
        return None

    return pre_tool_use_hook


def create_permission_handler(
    repo_path: str,
) -> Callable[
    [PermissionRequest, dict[str, str]],
    PermissionRequestResult,
]:
    """Create a permission handler that auto-approves operations within repo_path.

    - read/write within repo_path: approved
    - read/write outside repo_path: denied
    - shell: approved (needed for git, tests)
    - url: approved (needed for GitHub API)
    - mcp: approved

    Args:
        repo_path: The repository directory path to restrict operations to

    Returns:
        A permission handler function
    """

    def permission_handler(
        request: PermissionRequest,
        env: dict[str, str],  # noqa: ARG001
    ) -> PermissionRequestResult:
        """Handle permission requests based on operation kind and path.

        Args:
            request: The permission request from the SDK
            env: Environment variables (unused)

        Returns:
            PermissionRequestResult with approval or denial
        """
        kind = request.get("kind", "")

        # Always approve shell, url, and mcp operations
        if kind in ("shell", "url", "mcp"):
            return {
                "kind": "approved",
                "rules": [],
            }

        # For read/write operations, check if path is within repo
        if kind in ("read", "write"):
            # Extract path from request - structure varies by kind
            path = request.get("path") or request.get("file_path")

            if not path or not isinstance(path, str):
                # If we can't determine the path, deny for safety
                return {
                    "kind": "denied-by-rules",
                    "rules": [],
                }

            if _is_path_inside_repo(path, repo_path):
                return {
                    "kind": "approved",
                    "rules": [],
                }
            else:
                return {
                    "kind": "denied-by-rules",
                    "rules": [],
                }

        # Unknown operation kind - deny for safety
        return {
            "kind": "denied-by-rules",
            "rules": [],
        }

    return permission_handler
