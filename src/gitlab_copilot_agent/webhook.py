"""Webhook endpoint for GitLab MR events."""

import hmac

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from gitlab_copilot_agent.concurrency import ReviewedMRTracker
from gitlab_copilot_agent.metrics import webhook_errors_total, webhook_received_total
from gitlab_copilot_agent.models import MergeRequestWebhookPayload, NoteWebhookPayload
from gitlab_copilot_agent.mr_comment_handler import (
    handle_copilot_comment,
    is_approval_command,
    parse_copilot_command,
)
from gitlab_copilot_agent.orchestrator import handle_review

log = structlog.get_logger()

router = APIRouter()

HANDLED_ACTIONS = frozenset({"open", "update"})


def _validate_webhook_token(received: str | None, expected: str) -> None:
    if received is None or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook token")


async def _process_review(request: Request, payload: MergeRequestWebhookPayload) -> None:
    """Run review in background task; marks head SHA as reviewed on success."""
    settings = request.app.state.settings
    executor = request.app.state.executor
    review_tracker: ReviewedMRTracker = request.app.state.review_tracker
    mr = payload.object_attributes
    project_id = payload.project.id
    head_sha = mr.last_commit.id
    try:
        await handle_review(settings, payload, executor)
        review_tracker.mark(project_id, mr.iid, head_sha)
    except Exception:
        webhook_errors_total.add(1, {"handler": "review"})
        await log.aexception("background_review_failed")


async def _process_copilot_comment(request: Request, payload: NoteWebhookPayload) -> None:
    settings = request.app.state.settings
    executor = request.app.state.executor
    repo_locks = request.app.state.repo_locks
    approval_store = request.app.state.approval_store
    try:
        await handle_copilot_comment(settings, payload, executor, repo_locks, approval_store)
    except Exception:
        webhook_errors_total.add(1, {"handler": "copilot_comment"})
        await log.aexception("background_copilot_comment_failed")


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
        note_text = note_payload.object_attributes.note
        if not parse_copilot_command(note_text) and not is_approval_command(note_text):
            return {"status": "ignored", "reason": "not a /copilot command"}
        if (
            settings.agent_gitlab_username
            and note_payload.user.username == settings.agent_gitlab_username
        ):
            return {"status": "ignored", "reason": "self-comment"}
        background_tasks.add_task(_process_copilot_comment, request, note_payload)
        return {"status": "queued"}

    return {"status": "ignored", "reason": f"unhandled event: {object_kind}"}
