"""Tests for SDK-native sandboxing hooks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gitlab_copilot_agent.sandbox import (
    create_permission_handler,
    create_pre_tool_use_hook,
)

# Test constants
TEST_REPO_PATH = "/tmp/test-repo"
OUTSIDE_PATH = "/etc/passwd"
RELATIVE_INSIDE_PATH = "src/main.py"
ABSOLUTE_INSIDE_PATH = os.path.join(TEST_REPO_PATH, "src/main.py")


class TestPermissionHandler:
    """Test the permission handler for various operation kinds."""

    def test_approves_read_inside_repo(self, tmp_path: Path) -> None:
        """Permission handler approves read operations inside repo_path."""
        handler = create_permission_handler(str(tmp_path))

        test_file = tmp_path / "test.txt"
        request = {
            "kind": "read",
            "path": str(test_file),
            "toolCallId": "test-123",
        }

        result = handler(request, {})

        assert result["kind"] == "approved"

    def test_denies_read_outside_repo(self, tmp_path: Path) -> None:
        """Permission handler denies read operations outside repo_path."""
        handler = create_permission_handler(str(tmp_path))

        request = {
            "kind": "read",
            "path": OUTSIDE_PATH,
            "toolCallId": "test-123",
        }

        result = handler(request, {})

        assert result["kind"] == "denied-by-rules"

    def test_approves_write_inside_repo(self, tmp_path: Path) -> None:
        """Permission handler approves write operations inside repo_path."""
        handler = create_permission_handler(str(tmp_path))

        test_file = tmp_path / "output.txt"
        request = {
            "kind": "write",
            "path": str(test_file),
            "toolCallId": "test-123",
        }

        result = handler(request, {})

        assert result["kind"] == "approved"

    def test_denies_write_outside_repo(self, tmp_path: Path) -> None:
        """Permission handler denies write operations outside repo_path."""
        handler = create_permission_handler(str(tmp_path))

        request = {
            "kind": "write",
            "path": OUTSIDE_PATH,
            "toolCallId": "test-123",
        }

        result = handler(request, {})

        assert result["kind"] == "denied-by-rules"

    def test_approves_shell_operations(self, tmp_path: Path) -> None:
        """Permission handler approves shell operations."""
        handler = create_permission_handler(str(tmp_path))

        request = {
            "kind": "shell",
            "command": "git status",
            "toolCallId": "test-123",
        }

        result = handler(request, {})

        assert result["kind"] == "approved"

    def test_approves_url_operations(self, tmp_path: Path) -> None:
        """Permission handler approves url operations."""
        handler = create_permission_handler(str(tmp_path))

        request = {
            "kind": "url",
            "url": "https://api.github.com/repos/test/test",
            "toolCallId": "test-123",
        }

        result = handler(request, {})

        assert result["kind"] == "approved"

    def test_approves_mcp_operations(self, tmp_path: Path) -> None:
        """Permission handler approves mcp operations."""
        handler = create_permission_handler(str(tmp_path))

        request = {
            "kind": "mcp",
            "toolCallId": "test-123",
        }

        result = handler(request, {})

        assert result["kind"] == "approved"

    def test_denies_when_path_missing(self, tmp_path: Path) -> None:
        """Permission handler denies read/write when path is missing."""
        handler = create_permission_handler(str(tmp_path))

        request = {
            "kind": "read",
            "toolCallId": "test-123",
        }

        result = handler(request, {})

        assert result["kind"] == "denied-by-rules"

    def test_denies_unknown_operation_kind(self, tmp_path: Path) -> None:
        """Permission handler denies unknown operation kinds."""
        handler = create_permission_handler(str(tmp_path))

        request = {
            "kind": "unknown",
            "toolCallId": "test-123",
        }

        result = handler(request, {})

        assert result["kind"] == "denied-by-rules"


class TestPreToolUseHook:
    """Test the pre-tool-use hook for file operations."""

    def test_allows_read_inside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook allows read operations inside repo_path."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        test_file = tmp_path / "test.txt"
        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
            "toolArgs": {"path": str(test_file)},
        }

        result = hook(hook_input, {})

        # None means allow (default behavior)
        assert result is None

    def test_denies_read_outside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook denies read operations outside repo_path."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
            "toolArgs": {"path": OUTSIDE_PATH},
        }

        result = hook(hook_input, {})

        assert result is not None
        assert result["permissionDecision"] == "deny"
        assert "outside allowed repository" in result["permissionDecisionReason"].lower()

    def test_allows_write_inside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook allows write operations inside repo_path."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        test_file = tmp_path / "output.txt"
        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "write",
            "toolArgs": {"path": str(test_file)},
        }

        result = hook(hook_input, {})

        assert result is None

    def test_denies_write_outside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook denies write operations outside repo_path."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "write",
            "toolArgs": {"path": OUTSIDE_PATH},
        }

        result = hook(hook_input, {})

        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_allows_edit_inside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook allows edit operations inside repo_path."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        test_file = tmp_path / "edit.txt"
        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "edit",
            "toolArgs": {"path": str(test_file)},
        }

        result = hook(hook_input, {})

        assert result is None

    def test_denies_edit_outside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook denies edit operations outside repo_path."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "edit",
            "toolArgs": {"path": OUTSIDE_PATH},
        }

        result = hook(hook_input, {})

        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_allows_create_inside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook allows create operations inside repo_path."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        test_file = tmp_path / "new.txt"
        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "create",
            "toolArgs": {"path": str(test_file)},
        }

        result = hook(hook_input, {})

        assert result is None

    def test_denies_create_outside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook denies create operations outside repo_path."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "create",
            "toolArgs": {"path": OUTSIDE_PATH},
        }

        result = hook(hook_input, {})

        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_handles_relative_paths_inside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook correctly handles relative paths inside repo."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        # Create a subdirectory
        subdir = tmp_path / "src"
        subdir.mkdir()

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
            "toolArgs": {"path": "src/main.py"},
        }

        result = hook(hook_input, {})

        # Relative path inside repo should be allowed
        assert result is None

    def test_handles_relative_path_escape_attempt(self, tmp_path: Path) -> None:
        """Pre-tool-use hook denies relative paths that escape repo."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
            "toolArgs": {"path": "../../etc/passwd"},
        }

        result = hook(hook_input, {})

        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_handles_symlink_escape_attempt(self, tmp_path: Path) -> None:
        """Pre-tool-use hook denies symlinks pointing outside repo."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        # Create a symlink pointing outside repo
        symlink = tmp_path / "evil_link"
        try:
            symlink.symlink_to("/etc/passwd")
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not supported on this platform")

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
            "toolArgs": {"path": str(symlink)},
        }

        result = hook(hook_input, {})

        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_allows_non_file_tools(self, tmp_path: Path) -> None:
        """Pre-tool-use hook allows non-file tools like shell."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "bash",
            "toolArgs": {"command": "ls -la"},
        }

        result = hook(hook_input, {})

        # Non-file tools should be allowed
        assert result is None

    def test_handles_missing_tool_args(self, tmp_path: Path) -> None:
        """Pre-tool-use hook handles missing toolArgs gracefully."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
        }

        result = hook(hook_input, {})

        # Missing toolArgs should be allowed (let SDK handle validation)
        assert result is None

    def test_handles_empty_tool_args(self, tmp_path: Path) -> None:
        """Pre-tool-use hook handles empty toolArgs gracefully."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
            "toolArgs": {},
        }

        result = hook(hook_input, {})

        # Empty toolArgs should be allowed (let SDK handle validation)
        assert result is None

    def test_handles_invalid_tool_args_type(self, tmp_path: Path) -> None:
        """Pre-tool-use hook handles invalid toolArgs type gracefully."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
            "toolArgs": "invalid",
        }

        result = hook(hook_input, {})

        # Invalid toolArgs should be allowed (let SDK handle validation)
        assert result is None

    def test_handles_alternative_path_field(self, tmp_path: Path) -> None:
        """Pre-tool-use hook handles alternative path field names."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        test_file = tmp_path / "test.txt"
        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
            "toolArgs": {"file_path": str(test_file)},
        }

        result = hook(hook_input, {})

        # Should handle file_path field
        assert result is None

    def test_denies_alternative_path_field_outside_repo(self, tmp_path: Path) -> None:
        """Pre-tool-use hook denies alternative path field outside repo."""
        hook = create_pre_tool_use_hook(str(tmp_path))

        hook_input = {
            "timestamp": 1234567890,
            "cwd": str(tmp_path),
            "toolName": "read",
            "toolArgs": {"file_path": OUTSIDE_PATH},
        }

        result = hook(hook_input, {})

        assert result is not None
        assert result["permissionDecision"] == "deny"
