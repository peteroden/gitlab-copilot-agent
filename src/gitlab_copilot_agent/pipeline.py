"""Pipeline protocol — shared abstraction for all task pipelines.

Defines the four-stage contract (prepare → execute → process → cleanup)
and a generic runner with per-stage tracing and guarded error handling.

See ADR-0015.
"""

from __future__ import annotations

import time
from pathlib import Path  # noqa: TC003 — Pydantic runtime field type
from typing import Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict

from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer("pipeline")


# ---------------------------------------------------------------------------
# Context models
# ---------------------------------------------------------------------------


class BasePipelineContext(BaseModel):
    """Shared mutable state passed through pipeline stages.

    Each pipeline subclasses this with its own fields. The runner only
    touches ``repo_path`` (for cleanup logging) and ``outcome`` (for
    metrics / success detection).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    repo_path: Path | None = None
    outcome: str = "error"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Pipeline[PipelineContextT: BasePipelineContext](Protocol):
    """Four-stage pipeline protocol.

    Every task type implements these methods. ``run_pipeline`` calls them
    in order, wrapping each in a trace span.

    ``handle_error`` is invoked when prepare/execute/process raises.
    Implementations post user-visible error messages (MR comment, Jira
    comment, thread reply) — the runner does not know where errors go.
    """

    async def prepare(self, pipeline_context: PipelineContextT) -> None:
        """Clone repo, fetch context, build prompt."""
        ...

    async def execute(self, pipeline_context: PipelineContextT) -> None:
        """Run the LLM session via the executor."""
        ...

    async def process(self, pipeline_context: PipelineContextT) -> None:
        """Parse result, post output (comments, MR, Jira transition)."""
        ...

    async def cleanup(self, pipeline_context: PipelineContextT) -> None:
        """Remove temp files, record metrics. Must not raise."""
        ...

    async def handle_error(self, pipeline_context: PipelineContextT, exc: Exception) -> None:
        """Post user-visible error notification. Must not raise."""
        ...


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_pipeline[PipelineContextT: BasePipelineContext](
    pipeline: Pipeline[PipelineContextT], pipeline_context: PipelineContextT
) -> PipelineContextT:
    """Execute a pipeline through all four stages with tracing.

    Guarantees:
    - cleanup always runs (even on error)
    - cleanup exceptions are logged, never mask the primary exception
    - handle_error is called on failure before cleanup
    - outcome is set to ``"success"`` only if no stage raised AND the
      pipeline did not already set a more specific outcome
    """
    pipeline_name = type(pipeline).__name__
    start = time.monotonic()
    primary_exc: BaseException | None = None

    with _tracer.start_as_current_span(
        f"pipeline.{pipeline_name}",
    ):
        try:
            with _tracer.start_as_current_span("pipeline.prepare"):
                await pipeline.prepare(pipeline_context)
            with _tracer.start_as_current_span("pipeline.execute"):
                await pipeline.execute(pipeline_context)
            with _tracer.start_as_current_span("pipeline.process"):
                await pipeline.process(pipeline_context)

            # Only set "success" if the pipeline didn't set a specific outcome
            if pipeline_context.outcome == "error":
                pipeline_context.outcome = "success"

        except Exception as exc:
            primary_exc = exc
            try:
                await pipeline.handle_error(pipeline_context, exc)
            except Exception:
                log.warning(
                    "pipeline_handle_error_failed",
                    pipeline=pipeline_name,
                    exc_info=True,
                )
        except BaseException as exc:
            # CancelledError, KeyboardInterrupt — skip handle_error
            primary_exc = exc
        finally:
            try:
                with _tracer.start_as_current_span("pipeline.cleanup"):
                    await pipeline.cleanup(pipeline_context)
            except Exception:
                log.warning(
                    "pipeline_cleanup_failed",
                    pipeline=pipeline_name,
                    exc_info=True,
                )

        log.info(
            "pipeline_complete",
            pipeline=pipeline_name,
            outcome=pipeline_context.outcome,
            duration_s=round(time.monotonic() - start, 3),
        )

    if primary_exc is not None:
        raise primary_exc

    return pipeline_context
