"""FastAPI application entrypoint."""

import asyncio
import glob
import hmac
import ipaddress
import os
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import ValidationError
from starlette.responses import Response

from gitlab_copilot_agent.app_context import AppContext
from gitlab_copilot_agent.coding_pipeline import CodingTaskRunner
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.credential_registry import CredentialRegistry
from gitlab_copilot_agent.dedup import DeduplicationService
from gitlab_copilot_agent.git import CLONE_DIR_PREFIX
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.gitlab_poller import GitLabPoller
from gitlab_copilot_agent.gitlab_webhook import router as webhook_router
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

configure_logging()

log = structlog.get_logger()

# -- Ingress security constants --
ALLOWED_PATHS = frozenset({"/webhook", "/health", "/config/reload"})
MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
_RELOAD_COOLDOWN = 10  # seconds
_reload_timestamps: dict[str, float] = {}

# Type alias for pre-parsed network objects
_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_cidrs(raw: str) -> list[_IPNetwork]:
    """Parse comma-separated CIDR strings into IP network objects."""
    networks: list[_IPNetwork] = []
    for entry in raw.split(","):
        stripped = entry.strip()
        if stripped:
            networks.append(ipaddress.ip_network(stripped, strict=False))
    return networks


def _get_client_ip(
    request: Request,
    trusted_proxies: list[_IPNetwork],
) -> str:
    """Extract real client IP, trusting X-Forwarded-For only from trusted proxies.

    Uses rightmost-non-trusted-proxy algorithm per RFC 7239.
    """
    client_host = request.client.host if request.client else "unknown"
    if not trusted_proxies:
        return client_host

    try:
        client_addr = ipaddress.ip_address(client_host)
    except ValueError:
        return client_host

    # Only trust XFF if direct connection is from a trusted proxy
    if not any(client_addr in net for net in trusted_proxies):
        return client_host

    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return client_host

    # Walk from rightmost to leftmost, return first non-trusted IP
    parts = [p.strip() for p in xff.split(",")]
    for part in reversed(parts):
        try:
            addr = ipaddress.ip_address(part)
        except ValueError:
            continue
        if not any(addr in net for net in trusted_proxies):
            return str(addr)

    # All XFF entries are trusted — use direct connection
    return client_host


def _cleanup_stale_repos(clone_dir: str | None = None) -> None:
    """Remove leftover mr-review-* dirs from prior crashes."""
    base = clone_dir or tempfile.gettempdir()
    for d in glob.glob(f"{base}/{CLONE_DIR_PREFIX}*"):
        shutil.rmtree(d, ignore_errors=True)


def _create_executor(backend: str, settings: Settings | None = None) -> TaskExecutor:
    """Factory: create a TaskExecutor for the given backend.

    Args:
        backend: One of 'local', 'kubernetes', 'container_apps'.
        settings: Required for remote backends (provides Azure Storage config).

    Returns:
        Configured TaskExecutor instance.
    """
    # Local dispatch mode bypasses Azure entirely
    if backend == "local" or (settings and settings.dispatch_backend == "local"):
        return LocalTaskExecutor()
    if backend in ("kubernetes", "container_apps"):
        if settings is None:
            msg = f"Settings required for {backend} executor"
            raise ValueError(msg)
        from gitlab_copilot_agent.remote_executor import RemoteTaskExecutor

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
        timeout = settings.k8s_job_timeout if backend == "kubernetes" else settings.aca_job_timeout
        return RemoteTaskExecutor(result_store=store, task_queue=task_queue, job_timeout=timeout)
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
    """Application lifespan — create services, stash in AppContext."""
    init_telemetry()
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
    except ValidationError as exc:
        _print_config_errors(exc)
        sys.exit(1)
    _cleanup_stale_repos(settings.clone_dir)
    executor = _create_executor(settings.task_executor, settings)

    # Pre-compute IP network objects for middleware (avoid per-request parsing)
    app.state.webhook_ip_allowlist = _parse_cidrs(settings.webhook_ip_allowlist)
    app.state.trusted_proxies = _parse_cidrs(settings.trusted_proxies)

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

    # Shared concurrency primitives
    repo_locks = create_lock()
    dedup_backend = create_dedup(
        azure_storage_account_url=settings.azure_storage_account_url,
        azure_storage_connection_string=settings.azure_storage_connection_string,
    )
    dedup = DeduplicationService(
        dedup_backend,
        review_on_push=settings.gitlab_review_on_push,
    )
    creds = CredentialRegistry.from_env()

    # Build typed AppContext (immutable services)
    app_context = AppContext(
        settings=settings,
        executor=executor,
        repo_locks=repo_locks,
        dedup=dedup,
        credential_registry=creds,
        allowed_project_ids=(
            frozenset(allowed_project_ids) if allowed_project_ids is not None else None
        ),
    )
    app.state.app_context = app_context

    # Mutable state: project_registry and pollers stay on app.state
    # directly for hot-reload support (see /config/reload endpoint).
    poller: JiraPoller | None = None
    gl_poller: GitLabPoller | None = None
    jira_client: JiraClient | None = None
    project_registry: ProjectRegistry | None = None
    if settings.jira:
        jira_client = JiraClient(settings.jira.url, settings.jira.email, settings.jira.api_token)
        rendered = RenderedMap.model_validate_json(settings.jira.project_map_json)
        # Backfill global Jira status env vars into bindings that lack explicit
        # overrides — preserves backward compatibility for JSON configs authored
        # before per-project status fields were added.
        for binding in rendered.mappings.values():
            if binding.trigger_status == "AI Ready" and settings.jira.trigger_status != "AI Ready":
                binding.trigger_status = settings.jira.trigger_status
            if (
                binding.in_progress_status == "In Progress"
                and settings.jira.in_progress_status != "In Progress"
            ):
                binding.in_progress_status = settings.jira.in_progress_status
            if (
                binding.in_review_status == "In Review"
                and settings.jira.in_review_status != "In Review"
            ):
                binding.in_review_status = settings.jira.in_review_status
        try:
            project_registry = await ProjectRegistry.from_rendered_map(
                rendered, creds, settings.gitlab_url
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Failed to build project registry: {exc}") from exc
        gl_client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        handler = CodingTaskRunner(settings, gl_client, jira_client, executor, repo_locks)
        poller = JiraPoller(
            jira_client,
            settings.jira,
            project_registry,
            handler,
            allowed_project_ids,
            dedup=dedup,
        )
        await poller.start()
        await log.ainfo("jira_poller_started", interval=settings.jira.poll_interval)

    if settings.gitlab_poll and allowed_project_ids:
        gl_client_poll = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        gl_poller = GitLabPoller(
            gl_client=gl_client_poll,
            settings=settings,
            project_ids=allowed_project_ids,
            dedup=dedup,
            executor=executor,
            repo_locks=repo_locks,
            project_registry=project_registry,
            credential_registry=creds,
            poll_interval=settings.gitlab_poll_interval,
        )
        await gl_poller.start()
        await log.ainfo(
            "gitlab_poller_started",
            interval=settings.gitlab_poll_interval,
            projects=sorted(allowed_project_ids),
        )

    # Always set mutable state — even None — so direct attribute access
    # works without getattr() fallbacks in gitlab_webhook.py and config_reload.
    app.state.project_registry = project_registry
    app.state.jira_poller = poller
    app.state.gl_poller = gl_poller

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
    steps.append(("dedup_close", dedup.aclose()))
    steps.append(("repo_locks_close", repo_locks.aclose()))
    steps.append(("telemetry_flush", asyncio.to_thread(shutdown_telemetry)))

    num_steps = len(steps)
    per_step = settings.shutdown_timeout / max(num_steps, 1)
    timed_out: list[str] = []

    await log.ainfo("shutdown_started", steps=num_steps, per_step_timeout=round(per_step, 1))
    for name, coro in steps:
        try:
            await asyncio.wait_for(coro, timeout=per_step)  # pyright: ignore[reportArgumentType]
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


_is_development = os.environ.get("ENVIRONMENT") == "development"
app = FastAPI(
    title="GitLab Copilot Agent",
    lifespan=lifespan,
    docs_url="/docs" if _is_development else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _is_development else None,
)
app.include_router(webhook_router)


# -- Middleware (LIFO: last registered = first executed on request) --


@app.middleware("http")
async def ip_allowlist_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reject webhook requests from IPs outside the allowlist.

    Only active when webhook_ip_allowlist is non-empty.
    Non-webhook paths are always allowed through.
    """
    if request.url.path != "/webhook":
        return await call_next(request)

    allowlist: list[_IPNetwork] = getattr(request.app.state, "webhook_ip_allowlist", [])
    if not allowlist:
        return await call_next(request)

    trusted: list[_IPNetwork] = getattr(request.app.state, "trusted_proxies", [])
    client_ip = _get_client_ip(request, trusted)

    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        log.warning("webhook_ip_rejected", client_ip=client_ip, reason="invalid_ip")
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    if not any(addr in net for net in allowlist):
        log.warning("webhook_ip_rejected", client_ip=client_ip)
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    return await call_next(request)


@app.middleware("http")
async def restrict_paths(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Return 404 for paths not in the allowed set."""
    if request.url.path not in ALLOWED_PATHS:
        log.warning("path_rejected", path=request.url.path)
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return await call_next(request)


@app.middleware("http")
async def limit_body_size(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reject request bodies exceeding MAX_BODY_SIZE.

    Checks Content-Length header first (fast path), then wraps the ASGI
    receive callable to count bytes for chunked transfer encoding.
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_SIZE:
                log.warning(
                    "body_size_rejected",
                    content_length=int(content_length),
                    max=MAX_BODY_SIZE,
                )
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        except ValueError:
            pass  # malformed header — fall through to streaming check

    # Streaming byte counter for chunked encoding
    received = 0
    original_receive = request._receive  # type: ignore[reportPrivateUsage]  # noqa: SLF001

    async def counting_receive() -> Any:
        nonlocal received
        message = await original_receive()
        body = message.get("body", b"")
        received += len(body)
        if received > MAX_BODY_SIZE:
            log.warning("body_size_rejected", received=received, max=MAX_BODY_SIZE)
            raise HTTPException(status_code=413, detail="Request body too large")
        return message

    request._receive = counting_receive  # type: ignore[reportPrivateUsage]  # noqa: SLF001
    return await call_next(request)


FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
async def health() -> dict[str, object]:
    """Health check endpoint."""
    result: dict[str, object] = {"status": "ok"}
    gl_poller: GitLabPoller | None = app.state.gl_poller
    if gl_poller is not None:
        result["gitlab_poller"] = gl_poller.status()
    return result


@app.post("/config/reload", response_model=None)
async def config_reload(
    body: RenderedMap,
    request: Request,
) -> dict[str, object] | JSONResponse:
    """Reload the Jira project registry without restarting.

    Requires X-Admin-Token when admin_token is configured,
    otherwise falls back to X-Gitlab-Token (webhook secret).
    Rate-limited to one request per client IP every 10 seconds.
    """
    app_context: AppContext = request.app.state.app_context
    settings = app_context.settings

    # Extract client IP early for logging
    trusted: list[_IPNetwork] = getattr(request.app.state, "trusted_proxies", [])
    client_ip = _get_client_ip(request, trusted)

    # -- Admin auth (Step 3.4) --
    if settings.admin_token:
        received = request.headers.get("X-Admin-Token")
        if received is None or not hmac.compare_digest(received, settings.admin_token):
            log.warning("admin_auth_failed", client_ip=client_ip)
            raise HTTPException(status_code=401, detail="Invalid admin token")
    else:
        secret = settings.gitlab_webhook_secret
        if secret is None:
            raise HTTPException(status_code=403, detail="Webhook secret not configured")
        received = request.headers.get("X-Gitlab-Token")
        if received is None or not hmac.compare_digest(received, secret):
            log.warning("admin_auth_failed", client_ip=client_ip)
            raise HTTPException(status_code=401, detail="Invalid token")

    # -- Rate limiting (Step 3.3) --
    now = time.monotonic()
    last = _reload_timestamps.get(client_ip, 0.0)
    if now - last < _RELOAD_COOLDOWN:
        retry_after = int(_RELOAD_COOLDOWN - (now - last)) + 1
        log.warning("rate_limited", client_ip=client_ip, endpoint="/config/reload")
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limited"},
            headers={"Retry-After": str(retry_after)},
        )
    _reload_timestamps[client_ip] = now

    poller: JiraPoller | None = app.state.jira_poller
    if poller is None:
        return {"status": "error", "detail": "Jira poller not active"}
    creds = CredentialRegistry.from_env()
    try:
        registry = await ProjectRegistry.from_rendered_map(body, creds, settings.gitlab_url)
    except Exception as exc:
        await log.aerror("config_reload_failed", error=str(exc))
        return {"status": "error", "detail": "Invalid configuration — check server logs"}
    await poller.reload_registry(registry)
    app.state.project_registry = registry
    gl_poller: GitLabPoller | None = app.state.gl_poller
    if gl_poller is not None:
        gl_poller.update_project_registry(registry)
    return {
        "status": "ok",
        "jira_keys": sorted(registry.jira_keys()),
    }


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("gitlab_copilot_agent.main:app", host=host, port=port, log_config=None)
