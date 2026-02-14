"""FastAPI application entrypoint."""

import glob
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from gitlab_copilot_agent.coding_orchestrator import CodingOrchestrator
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.git_operations import CLONE_DIR_PREFIX
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.jira_client import JiraClient
from gitlab_copilot_agent.jira_poller import JiraPoller
from gitlab_copilot_agent.project_mapping import ProjectMap
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
        add_trace_context,
        emit_to_otel_logs,
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
)

log = structlog.get_logger()


def _cleanup_stale_repos() -> None:
    """Remove leftover /tmp/mr-review-* dirs from prior crashes."""
    for d in glob.glob(f"/tmp/{CLONE_DIR_PREFIX}*"):
        shutil.rmtree(d, ignore_errors=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_telemetry()
    _cleanup_stale_repos()
    settings = Settings()
    app.state.settings = settings

    poller: JiraPoller | None = None
    if settings.jira:
        jira_client = JiraClient(settings.jira.url, settings.jira.email, settings.jira.api_token)
        project_map = ProjectMap.model_validate_json(settings.jira.project_map_json)
        gl_client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        handler = CodingOrchestrator(settings, gl_client, jira_client)
        poller = JiraPoller(jira_client, settings.jira, project_map, handler)
        await poller.start()
        await log.ainfo("jira_poller_started", interval=settings.jira.poll_interval)

    await log.ainfo("service started", gitlab_url=settings.gitlab_url)
    yield

    if poller:
        await poller.stop()
    await log.ainfo("service stopped")
    shutdown_telemetry()


app = FastAPI(title="GitLab Copilot Agent", lifespan=lifespan)
app.include_router(webhook_router)
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
