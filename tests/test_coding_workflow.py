"""Tests for coding_workflow.apply_coding_result."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.coding_workflow import apply_coding_result
from gitlab_copilot_agent.task_executor import CodingResult, ReviewResult

_M = "gitlab_copilot_agent.coding_workflow"


class TestApplyCodingResult:
    async def test_noop_for_review_result(self) -> None:
        result = ReviewResult(summary="looks good")
        # Should return without doing anything
        await apply_coding_result(result, Path("/tmp/fake"))

    async def test_noop_for_empty_patch(self) -> None:
        result = CodingResult(summary="no changes", patch="", base_sha="abc123")
        await apply_coding_result(result, Path("/tmp/fake"))

    async def test_applies_patch_when_sha_matches(self) -> None:
        result = CodingResult(summary="fixed it", patch="diff content", base_sha="abc123" * 7)
        with (
            patch(f"{_M}.git_head_sha", AsyncMock(return_value="abc123" * 7)),
            patch(f"{_M}.git_apply_patch", AsyncMock()) as mock_apply,
        ):
            await apply_coding_result(result, Path("/tmp/repo"))
            mock_apply.assert_awaited_once_with(Path("/tmp/repo"), "diff content")

    async def test_raises_on_sha_mismatch(self) -> None:
        result = CodingResult(summary="fixed", patch="diff", base_sha="aaa" * 14)
        with (
            patch(f"{_M}.git_head_sha", AsyncMock(return_value="bbb" * 14)),
            pytest.raises(RuntimeError, match="Clone diverged"),
        ):
            await apply_coding_result(result, Path("/tmp/repo"))

    async def test_skips_sha_check_when_empty(self) -> None:
        result = CodingResult(summary="fixed", patch="diff content", base_sha="")
        with (
            patch(f"{_M}.git_head_sha", AsyncMock(return_value="abc123" * 7)),
            patch(f"{_M}.git_apply_patch", AsyncMock()) as mock_apply,
        ):
            await apply_coding_result(result, Path("/tmp/repo"))
            mock_apply.assert_awaited_once()
