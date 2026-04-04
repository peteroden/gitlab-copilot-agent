"""Webhook endpoint for GitLab MR events."""

from __future__ import annotations

import hmac
import re
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from gitlab_copilot_agent.concurrency import ReviewedMRTracker
from gitlab_copilot_agent.discussion_models import AgentIdentity
from gitlab_copilot_agent.discussion_orchestrator import handle_discussion_interaction
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.metrics import webhook_errors_total, webhook_received_total
from gitlab_copilot_agent.models import MergeRequestWebhookPayload, NoteWebhookPayload
from gitlab_copilot_agent.orchestrator import handle_review
from gitlab_copilot_agent.project_registry import ProjectRegistry

if TYPE_CHECKING:
    from gitlab_copilot_agent.credential_registry import CredentialRegistry

log = structlog.get_logger()

router = APIRouter()

HANDLED_ACTIONS = frozenset({"open", "update"})


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
    settings = request.app.state.settings
    executor = request.app.state.executor
    review_tracker: ReviewedMRTracker = request.app.state.review_tracker
    registry: ProjectRegistry | None = getattr(request.app.state, "project_registry", None)
    mr = payload.object_attributes
    project_id = payload.project.id
    head_sha = mr.last_commit.id
    project_token = _resolve_project_token(project_id, registry, settings.gitlab_token)
    credential_registry: CredentialRegistry | None = getattr(
        request.app.state, "credential_registry", None
    )
    bound = log.bind(project_id=project_id, mr_iid=mr.iid, head_sha=head_sha)
    bound.info("background_review_starting")
    try:
        await handle_review(
            settings,
            payload,
            executor,
            project_token=project_token,
            credential_registry=credential_registry,
        )
        review_tracker.mark(project_id, mr.iid, head_sha)
        # Also mark in shared dedup store so the poller won't re-review
        dedup_store = getattr(request.app.state, "dedup_store", None)
        if dedup_store is not None:
            review_key = f"review:{project_id}:{mr.iid}:{head_sha}"
            await dedup_store.mark_seen(review_key, ttl_seconds=86400)
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
        settings = request.app.state.settings
        registry: ProjectRegistry | None = getattr(request.app.state, "project_registry", None)
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
    note_key: str = "",
) -> None:
    """Process a discussion interaction in the background."""
    settings = request.app.state.settings
    executor = request.app.state.executor
    repo_locks = request.app.state.repo_locks
    registry: ProjectRegistry | None = getattr(request.app.state, "project_registry", None)
    project_token = _resolve_project_token(payload.project.id, registry, settings.gitlab_token)
    bound = log.bind(
        project_id=payload.project.id,
        mr_iid=payload.merge_request.iid if payload.merge_request else None,
        note_body=payload.object_attributes.note[:80],
    )
    bound.info("background_discussion_starting")
    try:
        await handle_discussion_interaction(
            settings,
            payload,
            executor,
            agent_identity,
            project_token=project_token,
            repo_locks=repo_locks,
        )
        bound.info("background_discussion_completed")
    except Exception:
        webhook_errors_total.add(1, {"handler": "discussion"})
        bound.exception("background_discussion_failed")
    finally:
        if note_key:
            dedup_store = getattr(request.app.state, "dedup_store", None)
            if dedup_store is not None:
                await dedup_store.mark_seen(note_key, ttl_seconds=86400)


@router.post("/webhook", status_code=200)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str | None = Header(default=None),
) -> dict[str, str]:
    settings = request.app.state.settings

    _validate_webhook_token(x_gitlab_token, settings.gitlab_webhook_secret)

    body = await request.json()

    # Project allowlist check
    allowed = request.app.state.allowed_project_ids
    if allowed is not None:
        project_id = body.get("project", {}).get("id")
        if project_id not in allowed:
            return {"status": "ignored", "reason": "project not in allowlist"}

    object_kind = body.get("object_kind")
    webhook_received_total.add(1, {"object_kind": object_kind or "unknown"})

    if object_kind == "merge_request":
        payload = MergeRequestWebhookPayload.model_validate(body)
        mr = payload.object_attributes
        action = mr.action
        if action not in HANDLED_ACTIONS:
            return {"status": "ignored", "reason": f"action '{action}' not handled"}

        # Skip title/description-only updates (no new commits)
        if action == "update" and mr.oldrev is None:
            return {"status": "ignored", "reason": "no new commits"}

        # Deduplicate by (project_id, mr_iid, head_sha)
        review_tracker: ReviewedMRTracker = request.app.state.review_tracker
        head_sha = mr.last_commit.id
        if review_tracker.is_reviewed(payload.project.id, mr.iid, head_sha):
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
        credential_registry: CredentialRegistry | None = getattr(
            request.app.state, "credential_registry", None
        )
        if credential_registry is None:
            return {"status": "ignored", "reason": "no credential registry"}

        # Resolve the credential_ref for this project (not always "default")
        registry: ProjectRegistry | None = getattr(request.app.state, "project_registry", None)
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
        dedup_store = getattr(request.app.state, "dedup_store", None)
        note_id = note_payload.object_attributes.id
        mr_iid = note_payload.merge_request.iid if note_payload.merge_request else 0
        note_key = f"note:{note_payload.project.id}:{mr_iid}:{note_id}"
        if dedup_store is not None and await dedup_store.is_seen(note_key):
            return {"status": "skipped", "reason": "already processed"}

        background_tasks.add_task(
            _process_discussion, request, note_payload, agent_identity, note_key
        )
        return {"status": "queued"}

    return {"status": "ignored", "reason": f"unhandled event: {object_kind}"}
