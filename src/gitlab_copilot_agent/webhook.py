"""Webhook endpoint for GitLab MR events."""

import hmac

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from gitlab_copilot_agent.models import MergeRequestWebhookPayload, NoteWebhookPayload
from gitlab_copilot_agent.mr_comment_handler import handle_copilot_comment, parse_copilot_command
from gitlab_copilot_agent.orchestrator import handle_review

log = structlog.get_logger()

router = APIRouter()

HANDLED_ACTIONS = frozenset({"open", "update"})


def _validate_webhook_token(received: str | None, expected: str) -> None:
    if received is None or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook token")


async def _process_review(request: Request, payload: MergeRequestWebhookPayload) -> None:
    """Run review in background task."""
    settings = request.app.state.settings
    try:
        await handle_review(settings, payload)
    except Exception:
        await log.aexception("background_review_failed")


async def _process_copilot_comment(request: Request, payload: NoteWebhookPayload) -> None:
    settings = request.app.state.settings
    repo_locks = request.app.state.repo_locks
    try:
        await handle_copilot_comment(settings, payload, repo_locks)
    except Exception:
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
    object_kind = body.get("object_kind")

    if object_kind == "merge_request":
        payload = MergeRequestWebhookPayload.model_validate(body)
        action = payload.object_attributes.action
        if action not in HANDLED_ACTIONS:
            return {"status": "ignored", "reason": f"action '{action}' not handled"}
        background_tasks.add_task(_process_review, request, payload)
        return {"status": "queued"}

    if object_kind == "note":
        note_payload = NoteWebhookPayload.model_validate(body)
        if note_payload.object_attributes.noteable_type != "MergeRequest":
            return {"status": "ignored", "reason": "not an MR note"}
        if not parse_copilot_command(note_payload.object_attributes.note):
            return {"status": "ignored", "reason": "not a /copilot command"}
        if (
            settings.agent_gitlab_username
            and note_payload.user.username == settings.agent_gitlab_username
        ):
            return {"status": "ignored", "reason": "self-comment"}
        background_tasks.add_task(_process_copilot_comment, request, note_payload)
        return {"status": "queued"}

    return {"status": "ignored", "reason": f"unhandled event: {object_kind}"}
