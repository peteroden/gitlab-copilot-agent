"""FastAPI application entrypoint."""

import glob
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.gitlab_client import CLONE_DIR_PREFIX
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _cleanup_stale_repos()
    settings = Settings()
    app.state.settings = settings
    await log.ainfo("service started", gitlab_url=settings.gitlab_url)
    yield
    await log.ainfo("service stopped")


app = FastAPI(title="GitLab Copilot Agent", lifespan=lifespan)
app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
