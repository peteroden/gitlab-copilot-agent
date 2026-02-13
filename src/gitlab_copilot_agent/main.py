"""FastAPI application entrypoint."""

import glob
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.gitlab_client import CLONE_DIR_PREFIX
from gitlab_copilot_agent.jira_client import JiraClient
from gitlab_copilot_agent.jira_models import JiraIssue
from gitlab_copilot_agent.jira_poller import JiraPoller
from gitlab_copilot_agent.project_mapping import GitLabProjectMapping, ProjectMap
from gitlab_copilot_agent.webhook import router as webhook_router

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
)

log = structlog.get_logger()


def _cleanup_stale_repos() -> None:
    """Remove leftover /tmp/mr-review-* dirs from prior crashes."""
    for d in glob.glob(f"/tmp/{CLONE_DIR_PREFIX}*"):
        shutil.rmtree(d, ignore_errors=True)


class _NoOpHandler:
    """Placeholder handler for Jira issues until orchestrator is wired in PR #9."""

    async def handle(
        self, issue: JiraIssue, project_mapping: GitLabProjectMapping
    ) -> None:
        await log.ainfo(
            "jira_issue_discovered",
            issue_key=issue.key,
            project=issue.project_key,
            gitlab_project=project_mapping.gitlab_project_id,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _cleanup_stale_repos()
    settings = Settings()
    app.state.settings = settings

    poller: JiraPoller | None = None
    if settings.jira:
        # Create Jira client and poller
        jira_client = JiraClient(
            settings.jira.url, settings.jira.email, settings.jira.api_token
        )
        project_map = ProjectMap.model_validate_json(settings.jira.project_map_json)
        # Use a no-op handler for now — orchestrator will be wired in PR #9
        poller = JiraPoller(jira_client, settings.jira, project_map, _NoOpHandler())
        await poller.start()
        await log.ainfo(
            "jira_poller_started", interval=settings.jira.poll_interval
        )

    await log.ainfo("service started", gitlab_url=settings.gitlab_url)
    yield

    if poller:
        await poller.stop()
    await log.ainfo("service stopped")


app = FastAPI(title="GitLab Copilot Agent", lifespan=lifespan)
app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
