"""FastAPI application entrypoint."""

import asyncio
import glob
import hmac
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import ValidationError

from gitlab_copilot_agent.coding_orchestrator import CodingOrchestrator
from gitlab_copilot_agent.concurrency import (
    ProcessedIssueTracker,
    ReviewedMRTracker,
)
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.credential_registry import CredentialRegistry
from gitlab_copilot_agent.git_operations import CLONE_DIR_PREFIX
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.gitlab_poller import GitLabPoller
from gitlab_copilot_agent.jira_client import JiraClient
from gitlab_copilot_agent.jira_poller import JiraPoller
from gitlab_copilot_agent.mapping_models import RenderedMap
from gitlab_copilot_agent.project_registry import ProjectRegistry
from gitlab_copilot_agent.state import (
    create_dedup,
    create_lock,
    create_result_store,
    create_task_queue,
)
from gitlab_copilot_agent.task_executor import LocalTaskExecutor, TaskExecutor
from gitlab_copilot_agent.telemetry import (
    configure_logging,
    init_telemetry,
    shutdown_telemetry,
)
from gitlab_copilot_agent.webhook import router as webhook_router

configure_logging()

log = structlog.get_logger()


def _cleanup_stale_repos(clone_dir: str | None = None) -> None:
    """Remove leftover mr-review-* dirs from prior crashes."""
    base = clone_dir or tempfile.gettempdir()
    for d in glob.glob(f"{base}/{CLONE_DIR_PREFIX}*"):
        shutil.rmtree(d, ignore_errors=True)


def _create_executor(backend: str, settings: Settings | None = None) -> TaskExecutor:
    """Factory: create a TaskExecutor for the given backend."""
    if backend == "kubernetes":
        if settings is None:
            msg = "Settings required for kubernetes executor"
            raise ValueError(msg)
        from gitlab_copilot_agent.k8s_executor import KubernetesTaskExecutor

        store = create_result_store(
            azure_storage_account_url=settings.azure_storage_account_url,
            azure_storage_connection_string=settings.azure_storage_connection_string,
            task_blob_container=settings.task_blob_container,
        )
        task_queue = create_task_queue(
            azure_storage_queue_url=settings.azure_storage_queue_url,
            azure_storage_account_url=settings.azure_storage_account_url,
            azure_storage_connection_string=settings.azure_storage_connection_string,
            task_queue_name=settings.task_queue_name,
            task_blob_container=settings.task_blob_container,
        )
        return KubernetesTaskExecutor(settings=settings, result_store=store, task_queue=task_queue)
    if backend == "container_apps":
        if settings is None:
            msg = "Settings required for container_apps executor"
            raise ValueError(msg)
        from gitlab_copilot_agent.aca_executor import ContainerAppsTaskExecutor

        task_queue = create_task_queue(
            azure_storage_queue_url=settings.azure_storage_queue_url,
            azure_storage_account_url=settings.azure_storage_account_url,
            azure_storage_connection_string=settings.azure_storage_connection_string,
            task_queue_name=settings.task_queue_name,
            task_blob_container=settings.task_blob_container,
        )
        store = create_result_store(
            azure_storage_account_url=settings.azure_storage_account_url,
            azure_storage_connection_string=settings.azure_storage_connection_string,
            task_blob_container=settings.task_blob_container,
        )
        return ContainerAppsTaskExecutor(
            settings=settings, result_store=store, task_queue=task_queue
        )
    return LocalTaskExecutor()


def _print_config_errors(exc: ValidationError) -> None:
    """Print a human-friendly summary of configuration errors to stderr."""
    lines = ["\n❌ Configuration error:\n"]
    for error in exc.errors():
        loc = ".".join(str(p) for p in error["loc"]) if error["loc"] else "?"
        env_var = loc.upper()
        desc = ""
        if loc in Settings.model_fields:
            desc = Settings.model_fields[loc].description or ""
        err_type = error["type"]
        if err_type == "missing":
            lines.append(f"  {env_var:<40} (missing) {desc}")
        elif err_type == "value_error":
            lines.append(f"  {error['msg']}")
        else:
            lines.append(f"  {env_var:<40} {error['msg']}")
    lines.append("\nSee docs/wiki/configuration-reference.md for all settings.\n")
    print("\n".join(lines), file=sys.stderr)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_telemetry()
    try:
        settings = Settings()
    except ValidationError as exc:
        _print_config_errors(exc)
        sys.exit(1)
    _cleanup_stale_repos(settings.clone_dir)
    app.state.settings = settings
    app.state.executor = _create_executor(settings.task_executor, settings)

    # Resolve project allowlist
    allowed_project_ids: set[int] | None = None
    if settings.gitlab_projects:
        gl_allowlist = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        allowed_project_ids = set()
        for entry in settings.gitlab_projects.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                project_id = await gl_allowlist.resolve_project(entry)
                allowed_project_ids.add(project_id)
            except Exception as exc:
                raise ValueError(f"Cannot resolve GitLab project: {entry!r}") from exc
        await log.ainfo("project_allowlist_resolved", project_ids=sorted(allowed_project_ids))
    app.state.allowed_project_ids = allowed_project_ids

    # Shared lock manager for both webhook and Jira flows
    repo_locks = create_lock()
    app.state.repo_locks = repo_locks
    dedup_store = create_dedup()
    app.state.dedup_store = dedup_store
    app.state.review_tracker = ReviewedMRTracker()

    poller: JiraPoller | None = None
    gl_poller: GitLabPoller | None = None
    jira_client: JiraClient | None = None
    if settings.jira:
        jira_client = JiraClient(settings.jira.url, settings.jira.email, settings.jira.api_token)
        rendered = RenderedMap.model_validate_json(settings.jira.project_map_json)
        creds = CredentialRegistry.from_env()
        try:
            project_registry = await ProjectRegistry.from_rendered_map(
                rendered, creds, settings.gitlab_url
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Failed to build project registry: {exc}") from exc
        gl_client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        tracker = ProcessedIssueTracker()
        handler = CodingOrchestrator(
            settings, gl_client, jira_client, app.state.executor, repo_locks, tracker
        )
        poller = JiraPoller(
            jira_client, settings.jira, project_registry, handler, allowed_project_ids
        )
        await poller.start()
        app.state.jira_poller = poller
        await log.ainfo("jira_poller_started", interval=settings.jira.poll_interval)

    if settings.gitlab_poll and allowed_project_ids:
        gl_client_poll = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        gl_poller = GitLabPoller(
            gl_client=gl_client_poll,
            settings=settings,
            project_ids=allowed_project_ids,
            dedup=dedup_store,
            executor=app.state.executor,
            repo_locks=repo_locks,
        )
        gl_poller._interval = settings.gitlab_poll_interval
        await gl_poller.start()
        app.state.gl_poller = gl_poller
        await log.ainfo(
            "gitlab_poller_started",
            interval=settings.gitlab_poll_interval,
            projects=sorted(allowed_project_ids),
        )

    await log.ainfo("service started", gitlab_url=settings.gitlab_url)
    yield

    # -- Graceful shutdown with per-step timeouts --
    steps: list[tuple[str, object]] = []
    if poller:
        steps.append(("jira_poller_stop", poller.stop()))
    if gl_poller:
        steps.append(("gitlab_poller_stop", gl_poller.stop()))
    if jira_client:
        steps.append(("jira_client_close", jira_client.close()))
    steps.append(("dedup_store_close", dedup_store.aclose()))
    steps.append(("repo_locks_close", repo_locks.aclose()))
    steps.append(("telemetry_flush", asyncio.to_thread(shutdown_telemetry)))

    num_steps = len(steps)
    per_step = settings.shutdown_timeout / max(num_steps, 1)
    timed_out: list[str] = []

    await log.ainfo("shutdown_started", steps=num_steps, per_step_timeout=round(per_step, 1))
    for name, coro in steps:
        try:
            await asyncio.wait_for(coro, timeout=per_step)  # type: ignore[arg-type]
            await log.ainfo("shutdown_step_done", step=name)
        except TimeoutError:
            timed_out.append(name)
            await log.awarning("shutdown_step_timeout", step=name, timeout=round(per_step, 1))
        except Exception:
            await log.awarning("shutdown_step_error", step=name, exc_info=True)

    await log.ainfo(
        "shutdown_complete",
        timed_out_steps=timed_out if timed_out else None,
    )


app = FastAPI(title="GitLab Copilot Agent", lifespan=lifespan)
app.include_router(webhook_router)
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
async def health() -> dict[str, object]:
    result: dict[str, object] = {"status": "ok"}
    gl_poller = getattr(app.state, "gl_poller", None)
    if gl_poller is not None:
        result["gitlab_poller"] = {
            "running": gl_poller._task is not None and not gl_poller._task.done(),
            "failures": gl_poller._failures,
            "watermark": gl_poller._watermark,
        }
    return result


@app.post("/config/reload")
async def config_reload(
    body: RenderedMap,
    request: Request,
) -> dict[str, object]:
    """Reload the Jira project registry without restarting.

    Requires the same webhook secret used for GitLab webhook auth.
    """
    settings: Settings = request.app.state.settings
    secret = settings.gitlab_webhook_secret
    received = request.headers.get("X-Gitlab-Token")
    if secret is None:
        raise HTTPException(status_code=403, detail="Webhook secret not configured")
    if received is None or not hmac.compare_digest(received, secret):
        raise HTTPException(status_code=401, detail="Invalid token")

    poller: JiraPoller | None = getattr(app.state, "jira_poller", None)
    if poller is None:
        return {"status": "error", "detail": "Jira poller not active"}
    creds = CredentialRegistry.from_env()
    try:
        registry = await ProjectRegistry.from_rendered_map(body, creds, settings.gitlab_url)
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}
    await poller.reload_registry(registry)
    return {
        "status": "ok",
        "jira_keys": sorted(registry.jira_keys()),
    }


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("gitlab_copilot_agent.main:app", host=host, port=port, log_config=None)
