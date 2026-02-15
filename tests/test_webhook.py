"""Tests for the webhook endpoint."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from tests.conftest import HEADERS, MR_IID, MR_PAYLOAD, PROJECT_ID, make_mr_payload


@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_webhook_rejects_bad_token(client: AsyncClient, token: str | None) -> None:
    headers = {"X-Gitlab-Token": token} if token else {}
    resp = await client.post("/webhook", json=MR_PAYLOAD, headers=headers)
    assert resp.status_code == 401


async def test_webhook_ignores_non_mr_event(client: AsyncClient) -> None:
    resp = await client.post("/webhook", json={"object_kind": "push"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


async def test_webhook_ignores_unhandled_action(client: AsyncClient) -> None:
    payload = make_mr_payload(action="merge")
    resp = await client.post("/webhook", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.parametrize("action", ["open", "update"])
async def test_webhook_queues_handled_actions(client: AsyncClient, action: str) -> None:
    payload = make_mr_payload(action=action)
    resp = await client.post("/webhook", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}


def _note_body(note: str = "/copilot fix the bug") -> dict[str, object]:
    return {
        "object_kind": "note",
        "user": {"id": 1, "username": "reviewer"},
        "project": {
            "id": PROJECT_ID,
            "path_with_namespace": "g/p",
            "git_http_url": "https://x.git",
        },
        "object_attributes": {"note": note, "noteable_type": "MergeRequest"},
        "merge_request": {
            "iid": MR_IID,
            "title": "Fix",
            "source_branch": "feat",
            "target_branch": "main",
        },
    }


async def test_note_webhook_queues_copilot_command(client: AsyncClient) -> None:
    with patch("gitlab_copilot_agent.webhook.handle_copilot_comment"):
        resp = await client.post("/webhook", json=_note_body(), headers=HEADERS)
    assert resp.json()["status"] == "queued"


async def test_note_webhook_ignores_non_copilot(client: AsyncClient) -> None:
    resp = await client.post("/webhook", json=_note_body("just a comment"), headers=HEADERS)
    assert resp.json()["status"] == "ignored"


async def test_note_webhook_uses_shared_lock_manager(client: AsyncClient) -> None:
    """Verify that the webhook endpoint uses app.state.repo_locks."""
    mock_handle = AsyncMock()
    with patch("gitlab_copilot_agent.webhook.handle_copilot_comment", mock_handle):
        resp = await client.post("/webhook", json=_note_body(), headers=HEADERS)
        assert resp.json()["status"] == "queued"

        # Wait for background task to complete
        import asyncio

        await asyncio.sleep(0.1)

        # Verify handle_copilot_comment was called with the lock manager from app state
        mock_handle.assert_awaited_once()
        args, kwargs = mock_handle.call_args
        # Third argument should be the repo_locks from app.state
        from gitlab_copilot_agent.main import app

        assert args[2] is app.state.repo_locks
