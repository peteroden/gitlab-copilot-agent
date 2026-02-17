"""FastAPI application entrypoint."""

import glob
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from gitlab_copilot_agent.coding_orchestrator import CodingOrchestrator
from gitlab_copilot_agent.concurrency import (
    ProcessedIssueTracker,
    ReviewedMRTracker,
)
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.git_operations import CLONE_DIR_PREFIX
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.jira_client import JiraClient
from gitlab_copilot_agent.jira_poller import JiraPoller
from gitlab_copilot_agent.project_mapping import ProjectMap
from gitlab_copilot_agent.redis_state import create_lock
from gitlab_copilot_agent.task_executor import LocalTaskExecutor, TaskExecutor
from gitlab_copilot_agent.telemetry import (
    add_trace_context,
    emit_to_otel_logs,
    init_telemetry,
    shutdown_telemetry,
)
from gitlab_copilot_agent.webhook import router as webhook_router

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        add_trace_context,  # type: ignore[list-item]
        emit_to_otel_logs,  # type: ignore[list-item]
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
)

log = structlog.get_logger()


def _cleanup_stale_repos(clone_dir: str | None = None) -> None:
    """Remove leftover mr-review-* dirs from prior crashes."""
    base = clone_dir or tempfile.gettempdir()
    for d in glob.glob(f"{base}/{CLONE_DIR_PREFIX}*"):
        shutil.rmtree(d, ignore_errors=True)


def _create_executor(backend: str) -> TaskExecutor:
    """Factory: create a TaskExecutor for the given backend."""
    if backend == "kubernetes":
        msg = "kubernetes task executor not yet implemented (see #81)"
        raise NotImplementedError(msg)
    return LocalTaskExecutor()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_telemetry()
    settings = Settings()
    _cleanup_stale_repos(settings.clone_dir)
    app.state.settings = settings
    app.state.executor = _create_executor(settings.task_executor)

    # Shared lock manager for both webhook and Jira flows
    repo_locks = create_lock(settings.state_backend, settings.redis_url)
    app.state.repo_locks = repo_locks
    app.state.review_tracker = ReviewedMRTracker()

    poller: JiraPoller | None = None
    jira_client: JiraClient | None = None
    if settings.jira:
        jira_client = JiraClient(settings.jira.url, settings.jira.email, settings.jira.api_token)
        project_map = ProjectMap.model_validate_json(settings.jira.project_map_json)
        gl_client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        tracker = ProcessedIssueTracker()
        handler = CodingOrchestrator(
            settings, gl_client, jira_client, app.state.executor, repo_locks, tracker
        )
        poller = JiraPoller(jira_client, settings.jira, project_map, handler)
        await poller.start()
        await log.ainfo("jira_poller_started", interval=settings.jira.poll_interval)

    await log.ainfo("service started", gitlab_url=settings.gitlab_url)
    yield

    if poller:
        await poller.stop()
    if jira_client:
        await jira_client.close()
    await repo_locks.aclose()
    await log.ainfo("service stopped")
    shutdown_telemetry()


app = FastAPI(title="GitLab Copilot Agent", lifespan=lifespan)
app.include_router(webhook_router)
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
