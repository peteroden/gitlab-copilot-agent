"""Webhook endpoint for GitLab MR events."""

from __future__ import annotations

import hmac
import re

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from gitlab_copilot_agent.app_context import get_app_context
from gitlab_copilot_agent.discussion_models import AgentIdentity
from gitlab_copilot_agent.discussion_pipeline import DiscussionContext, DiscussionPipeline
from gitlab_copilot_agent.events import TaskEvent
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.metrics import webhook_errors_total, webhook_received_total
from gitlab_copilot_agent.models import MergeRequestWebhookPayload, NoteWebhookPayload
from gitlab_copilot_agent.pipeline import run_pipeline
from gitlab_copilot_agent.project_registry import ProjectRegistry
from gitlab_copilot_agent.review_pipeline import ReviewContext, ReviewPipeline
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)

router = APIRouter()

HANDLED_ACTIONS = frozenset({"open", "update", "reopen"})


def _resolve_project_token(
    project_id: int,
    registry: ProjectRegistry | None,
    fallback_token: str,
) -> str:
    """Return per-project token from registry, or fall back to global token."""
    if registry is not None:
        resolved = registry.get_by_project_id(project_id)
        if resolved is not None:
            log.info(
                "credential_resolved",
                project_id=project_id,
                credential_ref=resolved.credential_ref,
                source="project_registry",
            )
            return resolved.token
    log.info("credential_resolved", project_id=project_id, source="global_fallback")
    return fallback_token


def _validate_webhook_token(received: str | None, expected: str | None) -> None:
    if expected is None:
        raise HTTPException(status_code=403, detail="Webhook secret not configured")
    if received is None or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook token")


async def _process_review(request: Request, payload: MergeRequestWebhookPayload) -> None:
    """Run review in background task; marks head SHA as reviewed on success."""
    app_context = get_app_context(request)
    settings = app_context.settings
    executor = app_context.executor
    credential_registry = app_context.credential_registry
    registry: ProjectRegistry | None = request.app.state.project_registry
    mr = payload.object_attributes
    project_id = payload.project.id
    head_sha = mr.last_commit.id
    project_token = _resolve_project_token(project_id, registry, settings.gitlab_token)
    resolution_behavior = settings.resolution_behavior
    credential_ref = "default"
    if registry is not None:
        resolved = registry.get_by_project_id(project_id)
        if resolved is not None:
            resolution_behavior = resolved.resolution_behavior
            credential_ref = resolved.credential_ref

    event = TaskEvent(
        task_type="review",
        project_id=project_id,
        repo=payload.project.path_with_namespace,
        clone_url=payload.project.git_http_url,
        branch=mr.source_branch,
        target_branch=mr.target_branch,
        mr_iid=mr.iid,
        head_sha=head_sha,
        trigger_source="webhook",
        token=project_token,
        credential_ref=credential_ref,
        resolution_behavior=resolution_behavior,
    )

    bound = log.bind(**event.log_safe())
    bound.info("background_review_starting")
    try:
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
        await app_context.dedup.mark_review(project_id, mr.iid, head_sha)
        bound.info("background_review_completed")
    except Exception:
        webhook_errors_total.add(1, {"handler": "review"})
        bound.exception("background_review_failed")


async def _is_agent_directed(
    payload: NoteWebhookPayload,
    agent_identity: AgentIdentity,
    request: Request,
) -> bool:
    """Check if a note is directed at the agent via @mention or thread participation.

    Returns True if:
    - The note body contains @{agent_username} (word-boundary regex), OR
    - The note is in a discussion thread where the agent previously commented
      (requires fetching the discussion via GitLab API).
    """
    note_body = payload.object_attributes.note
    pattern = rf"(?<![.\w-])@{re.escape(agent_identity.username)}(?![.\w-])"
    if re.search(pattern, note_body):
        return True

    # Check thread participation — if the note has a discussion_id,
    # fetch the discussion and check if the agent has a prior note in it
    discussion_id = payload.object_attributes.discussion_id
    if discussion_id and payload.merge_request:
        app_context = get_app_context(request)
        settings = app_context.settings
        registry: ProjectRegistry | None = request.app.state.project_registry
        token = _resolve_project_token(payload.project.id, registry, settings.gitlab_token)
        try:
            gl_client = GitLabClient(settings.gitlab_url, token)
            discussions = await gl_client.list_mr_discussions(
                payload.project.id, payload.merge_request.iid
            )
            for disc in discussions:
                if disc.discussion_id == discussion_id:
                    for note in disc.notes:
                        if note.author_id == agent_identity.user_id:
                            return True
                    break
        except Exception:
            await log.awarning("thread_participation_check_failed", exc_info=True)

    return False


async def _process_discussion(
    request: Request,
    payload: NoteWebhookPayload,
    agent_identity: AgentIdentity,
    note_id: int = 0,
    mr_iid: int = 0,
) -> None:
    """Process a discussion interaction in the background."""
    app_context = get_app_context(request)
    settings = app_context.settings
    executor = app_context.executor
    repo_locks = app_context.repo_locks
    registry: ProjectRegistry | None = request.app.state.project_registry
    project_token = _resolve_project_token(payload.project.id, registry, settings.gitlab_token)

    # Resolve resolution behavior from project registry or global settings
    resolution_behavior = settings.resolution_behavior
    credential_ref = "default"
    if registry is not None:
        resolved_project = registry.get_by_project_id(payload.project.id)
        if resolved_project is not None:
            resolution_behavior = resolved_project.resolution_behavior
            credential_ref = resolved_project.credential_ref

    mr_iid = payload.merge_request.iid if payload.merge_request else 0
    event = TaskEvent(
        task_type="discussion",
        project_id=payload.project.id,
        repo=payload.project.path_with_namespace,
        clone_url=payload.project.git_http_url,
        branch=payload.merge_request.source_branch,
        target_branch=payload.merge_request.target_branch,
        mr_iid=mr_iid,
        trigger_source="webhook",
        token=project_token,
        credential_ref=credential_ref,
        resolution_behavior=resolution_behavior,
        note_id=payload.object_attributes.id,
        discussion_id=payload.object_attributes.discussion_id,
        note_body=payload.object_attributes.note,
    )

    bound = log.bind(
        project_id=payload.project.id,
        mr_iid=mr_iid,
        note_body=payload.object_attributes.note[:80],
    )
    bound.info("background_discussion_starting")
    try:
        with _tracer.start_as_current_span(
            "mr.discussion_interaction",
            attributes={"project_id": event.project_id, "mr_iid": event.mr_iid or 0},
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
        bound.info("background_discussion_completed")
    except Exception:
        webhook_errors_total.add(1, {"handler": "discussion"})
        bound.exception("background_discussion_failed")
    finally:
        if note_id:
            await app_context.dedup.mark_note(payload.project.id, mr_iid, note_id)


@router.post("/webhook", status_code=200)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str | None = Header(default=None),
) -> dict[str, str]:
    """Handle GitLab webhook events (MR and note)."""
    app_context = get_app_context(request)
    settings = app_context.settings

    _validate_webhook_token(x_gitlab_token, settings.gitlab_webhook_secret)

    body = await request.json()

    # Project allowlist check
    if app_context.allowed_project_ids is not None:
        project_id = body.get("project", {}).get("id")
        if project_id not in app_context.allowed_project_ids:
            return {"status": "ignored", "reason": "project not in allowlist"}

    object_kind = body.get("object_kind")
    webhook_received_total.add(1, {"object_kind": object_kind or "unknown"})

    if object_kind == "merge_request":
        payload = MergeRequestWebhookPayload.model_validate(body)
        mr = payload.object_attributes
        action = mr.action
        if action not in HANDLED_ACTIONS:
            return {"status": "ignored", "reason": f"action '{action}' not handled"}

        # Skip title/description-only updates (no new commits).
        # Note: "reopen" has no oldrev — it passes through to review.
        # Diff scope is determined by SHA marker presence in review pipeline.
        if action == "update" and mr.oldrev is None:
            return {"status": "ignored", "reason": "no new commits"}

        # Deduplicate by (project_id, mr_iid, head_sha)
        head_sha = mr.last_commit.id
        if await app_context.dedup.is_review_seen(payload.project.id, mr.iid, head_sha):
            await log.ainfo(
                "review_skipped",
                reason="duplicate_head_sha",
                project_id=payload.project.id,
                mr_iid=mr.iid,
                head_sha=head_sha,
            )
            return {"status": "skipped", "reason": "already reviewed"}

        background_tasks.add_task(_process_review, request, payload)
        return {"status": "queued"}

    if object_kind == "note":
        note_payload = NoteWebhookPayload.model_validate(body)
        if note_payload.object_attributes.noteable_type != "MergeRequest":
            return {"status": "ignored", "reason": "not an MR note"}

        # Self-comment detection via agent identity (per-project credential)
        credential_registry = app_context.credential_registry

        # Resolve the credential_ref for this project (not always "default")
        registry: ProjectRegistry | None = request.app.state.project_registry
        credential_ref = "default"
        if registry is not None:
            resolved = registry.get_by_project_id(note_payload.project.id)
            if resolved is not None:
                credential_ref = resolved.credential_ref

        try:
            agent_identity = await credential_registry.resolve_identity(
                credential_ref, settings.gitlab_url
            )
        except Exception:
            await log.awarning("identity_resolution_failed", exc_info=True)
            return {"status": "ignored", "reason": "identity resolution failed"}

        if note_payload.user.id == agent_identity.user_id:
            return {"status": "ignored", "reason": "self-comment"}

        # Check if this note is directed at the agent
        if not await _is_agent_directed(note_payload, agent_identity, request):
            return {"status": "ignored", "reason": "not directed at agent"}

        # Deduplicate — prevents reprocessing on duplicate webhook deliveries
        note_id = note_payload.object_attributes.id
        mr_iid = note_payload.merge_request.iid if note_payload.merge_request else 0
        if await app_context.dedup.is_note_seen(note_payload.project.id, mr_iid, note_id):
            return {"status": "skipped", "reason": "already processed"}

        background_tasks.add_task(
            _process_discussion, request, note_payload, agent_identity, note_id, mr_iid
        )
        return {"status": "queued"}

    return {"status": "ignored", "reason": f"unhandled event: {object_kind}"}
