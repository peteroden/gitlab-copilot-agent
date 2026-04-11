"""Unified discussion handler — thread interactions via @mention or reply.

Thin delegation layer: validates inputs, acquires repo locks, constructs
the DiscussionPipeline, and calls ``run_pipeline()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.discussion_models import AgentIdentity  # noqa: TC001
from gitlab_copilot_agent.discussion_pipeline import DiscussionContext, DiscussionPipeline
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.pipeline import run_pipeline
from gitlab_copilot_agent.telemetry import get_tracer

if TYPE_CHECKING:
    from gitlab_copilot_agent.concurrency import DistributedLock
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.events import TaskEvent
    from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()
_tracer = get_tracer(__name__)


async def handle_discussion_interaction(
    settings: Settings,
    event: TaskEvent,
    executor: TaskExecutor,
    agent_identity: AgentIdentity,
    repo_locks: DistributedLock | None = None,
) -> None:
    """Handle an @mention or thread-reply interaction on an MR.

    Full pipeline: clone → fetch context → build prompt → LLM → post reply.
    If the LLM returns code changes, also commit and push.
    """
    with _tracer.start_as_current_span(
        "mr.discussion_interaction",
        attributes={
            "project_id": event.project_id,
            "mr_iid": event.mr_iid or 0,
        },
    ):
        gl_client = GitLabClient(settings.gitlab_url, event.token)
        pipeline = DiscussionPipeline(
            settings=settings,
            event=event,
            executor=executor,
            gl_client=gl_client,
            agent_identity=agent_identity,
        )

        async def _execute() -> None:
            await run_pipeline(pipeline, DiscussionContext())

        if repo_locks:
            async with repo_locks.acquire(event.clone_url):
                await _execute()
        else:
            await _execute()
