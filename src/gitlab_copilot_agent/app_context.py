"""Typed application context — replaces app.state service locator.

Created once during app lifespan and stashed on ``app.state.app_context``.
Consumers access via ``get_app_context(request)`` FastAPI dependency.

See ADR-0011.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TC002 — runtime import required for FastAPI Depends()

if TYPE_CHECKING:
    from gitlab_copilot_agent.concurrency import DistributedLock
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.credential_registry import CredentialRegistry
    from gitlab_copilot_agent.dedup import DeduplicationService
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
    dedup: DeduplicationService
    credential_registry: CredentialRegistry
    allowed_project_ids: frozenset[int] | None = field(default=None)


def get_app_context(request: Request) -> AppContext:
    """FastAPI dependency — retrieve the AppContext from app.state.

    Usage::

        @router.post("/webhook")
        async def webhook(app_context: AppContext = Depends(get_app_context)):
            ...

    Raises:
        RuntimeError: If the AppContext hasn't been initialized (lifespan bug
            or missing test fixture setup).
    """
    app_context: AppContext | None = getattr(request.app.state, "app_context", None)
    if app_context is None:
        msg = "AppContext not initialized — check lifespan or test fixture setup"
        raise RuntimeError(msg)
    return app_context
