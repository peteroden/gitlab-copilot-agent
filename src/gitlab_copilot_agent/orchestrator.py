"""Orchestrator — wires webhook → clone → review → post.

Thin delegation layer: validates inputs, constructs the ReviewPipeline,
and calls ``run_pipeline()``.  All logic lives in ``review_pipeline.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.pipeline import run_pipeline
from gitlab_copilot_agent.review_pipeline import ReviewContext, ReviewPipeline
from gitlab_copilot_agent.telemetry import get_tracer

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.credential_registry import CredentialRegistry
    from gitlab_copilot_agent.events import TaskEvent
    from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()
_tracer = get_tracer(__name__)


async def handle_review(
    settings: Settings,
    event: TaskEvent,
    executor: TaskExecutor,
    credential_registry: CredentialRegistry | None = None,
) -> None:
    """Full review pipeline: clone → review → parse → post comments."""
    with _tracer.start_as_current_span(
        "mr.review",
        attributes={"project_id": event.project_id, "mr_iid": event.mr_iid or 0},
    ):
        gl_client = GitLabClient(settings.gitlab_url, event.token)
        pipeline = ReviewPipeline(
            settings=settings,
            event=event,
            executor=executor,
            gl_client=gl_client,
            credential_registry=credential_registry,
        )
        await run_pipeline(pipeline, ReviewContext())
