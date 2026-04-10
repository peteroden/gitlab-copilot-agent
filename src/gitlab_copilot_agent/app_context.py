"""Typed application context — replaces app.state service locator.

Created once during app lifespan and stashed on ``app.state.ctx``.
Consumers access via ``get_services(request)`` FastAPI dependency.

See ADR-0011.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TC002 — runtime import required for FastAPI Depends()

if TYPE_CHECKING:
    from gitlab_copilot_agent.concurrency import (
        DeduplicationStore,
        DistributedLock,
        ReviewedMRTracker,
    )
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.credential_registry import CredentialRegistry
    from gitlab_copilot_agent.task_executor import TaskExecutor


@dataclass(frozen=True)
class AppContext:
    """All injected services in one typed object.

    Holds immutable service references created during app lifespan.
    Mutable state (project_registry, pollers) lives on ``app.state``
    directly — see ``/config/reload`` endpoint for hot-swap rationale.
    """

    settings: Settings
    executor: TaskExecutor
    repo_locks: DistributedLock
    dedup_store: DeduplicationStore
    review_tracker: ReviewedMRTracker
    credential_registry: CredentialRegistry
    allowed_project_ids: frozenset[int] | None = field(default=None)


def get_services(request: Request) -> AppContext:
    """FastAPI dependency — retrieve the AppContext from app.state.

    Usage::

        @router.post("/webhook")
        async def webhook(ctx: AppContext = Depends(get_services)):
            ...

    Raises:
        RuntimeError: If the AppContext hasn't been initialized (lifespan bug
            or missing test fixture setup).
    """
    ctx: AppContext | None = getattr(request.app.state, "ctx", None)
    if ctx is None:
        msg = "AppContext not initialized — check lifespan or test fixture setup"
        raise RuntimeError(msg)
    return ctx
