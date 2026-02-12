"""Webhook endpoint for GitLab MR events."""

import hmac

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from gitlab_copilot_agent.models import MergeRequestWebhookPayload
from gitlab_copilot_agent.orchestrator import handle_review

log = structlog.get_logger()

router = APIRouter()

HANDLED_ACTIONS = frozenset({"open", "update"})


def _validate_webhook_token(received: str | None, expected: str) -> None:
    if received is None or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code = 401, detail="Invalid webhook token")


async def _process_review(request: Request, payload: MergeRequestWebhookPayload) -> None:
    """Run review in background task."""
    settings = request.app.state.settings
    try:
        await handle_review(settings, payload)
    except Exception:
        await log.aexception("background_review_failed")


@router.post("/webhook", status_code=200)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str | None = Header(default=None),
) -> dict[str, str]:
    settings = request.app.state.settings

    _validate_webhook_token(x_gitlab_token, settings.gitlab_webhook_secret)

    body = await request.json()

    if body.get("object_kind") != "merge_request":
        return {"status": "ignored", "reason": "not a merge request event"}

    payload = MergeRequestWebhookPayload.model_validate(body)
    action = payload.object_attributes.action

    if action not in HANDLED_ACTIONS:
        return {"status": "ignored", "reason": f"action '{action}' not handled"}

    background_tasks.add_task(_process_review, request, payload)

    return {"status": "queued"}
