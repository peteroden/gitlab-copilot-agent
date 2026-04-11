"""Tests for pipeline protocol and run_pipeline() runner."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from gitlab_copilot_agent.pipeline import BasePipelineContext, run_pipeline

STAGE_NAMES = ("prepare", "execute", "process")


class _StubPipeline:
    """Minimal pipeline implementation for testing the runner."""

    def __init__(self) -> None:
        self.prepare = AsyncMock()
        self.execute = AsyncMock()
        self.process = AsyncMock()
        self.cleanup = AsyncMock()
        self.handle_error = AsyncMock()


class TestRunPipeline:
    """Tests for run_pipeline() execution semantics."""

    async def test_success_calls_all_stages_in_order(self) -> None:
        pipeline = _StubPipeline()
        pipeline_context = BasePipelineContext()

        result = await run_pipeline(pipeline, pipeline_context)  # type: ignore[arg-type]

        for stage in STAGE_NAMES:
            getattr(pipeline, stage).assert_awaited_once_with(pipeline_context)
        pipeline.cleanup.assert_awaited_once_with(pipeline_context)
        pipeline.handle_error.assert_not_awaited()
        assert result.outcome == "success"

    @pytest.mark.parametrize("failing_stage", STAGE_NAMES)
    async def test_stage_failure_calls_handle_error_and_cleanup(self, failing_stage: str) -> None:
        """Any stage failure → handle_error + cleanup + exception propagates."""
        pipeline = _StubPipeline()
        error = RuntimeError(f"{failing_stage} failed")
        getattr(pipeline, failing_stage).side_effect = error
        pipeline_context = BasePipelineContext()

        with pytest.raises(RuntimeError, match=f"{failing_stage} failed"):
            await run_pipeline(pipeline, pipeline_context)  # type: ignore[arg-type]

        pipeline.handle_error.assert_awaited_once_with(pipeline_context, error)
        pipeline.cleanup.assert_awaited_once()
        assert pipeline_context.outcome == "error"

    @pytest.mark.parametrize(
        ("broken_hook", "expect_match"),
        [
            ("cleanup", "primary error"),
            ("handle_error", "primary error"),
        ],
        ids=["cleanup_failure", "handle_error_failure"],
    )
    async def test_hook_failure_never_masks_primary_exception(
        self, broken_hook: str, expect_match: str
    ) -> None:
        """Neither cleanup nor handle_error failures hide the original error."""
        pipeline = _StubPipeline()
        pipeline.execute.side_effect = RuntimeError("primary error")
        getattr(pipeline, broken_hook).side_effect = RuntimeError(f"{broken_hook} boom")
        pipeline_context = BasePipelineContext()

        with pytest.raises(RuntimeError, match=expect_match):
            await run_pipeline(pipeline, pipeline_context)  # type: ignore[arg-type]

        pipeline.cleanup.assert_awaited_once()

    async def test_cleanup_failure_on_success_still_succeeds(self) -> None:
        """Cleanup exception is swallowed; outcome stays 'success'."""
        pipeline = _StubPipeline()
        pipeline.cleanup.side_effect = RuntimeError("cleanup boom")
        pipeline_context = BasePipelineContext()

        result = await run_pipeline(pipeline, pipeline_context)  # type: ignore[arg-type]

        assert result.outcome == "success"
        pipeline.handle_error.assert_not_awaited()

    async def test_pipeline_specific_outcome_preserved(self) -> None:
        """Runner does not overwrite a pipeline-set outcome (e.g. 'no_changes')."""
        pipeline = _StubPipeline()

        async def set_outcome(pc: BasePipelineContext) -> None:
            pc.outcome = "no_changes"

        pipeline.process.side_effect = set_outcome

        result = await run_pipeline(pipeline, BasePipelineContext())  # type: ignore[arg-type]
        assert result.outcome == "no_changes"

    async def test_cancelled_error_skips_handle_error(self) -> None:
        """CancelledError propagates without calling handle_error."""
        pipeline = _StubPipeline()
        pipeline.execute.side_effect = asyncio.CancelledError()
        pipeline_context = BasePipelineContext()

        with pytest.raises(asyncio.CancelledError):
            await run_pipeline(pipeline, pipeline_context)  # type: ignore[arg-type]

        pipeline.handle_error.assert_not_awaited()
        pipeline.cleanup.assert_awaited_once()
