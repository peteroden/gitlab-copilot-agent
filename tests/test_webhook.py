"""Tests for the webhook endpoint."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from gitlab_copilot_agent.main import app
from tests.conftest import HEADERS, MR_IID, MR_PAYLOAD, PROJECT_ID, make_mr_payload, make_settings

NON_ALLOWED_PROJECT_ID = 999


@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_webhook_rejects_bad_token(client: AsyncClient, token: str | None) -> None:
    headers = {"X-Gitlab-Token": token} if token else {}
    resp = await client.post("/webhook", json=MR_PAYLOAD, headers=headers)
    assert resp.status_code == 401


async def test_webhook_returns_403_when_secret_not_configured(client: AsyncClient) -> None:
    """Polling-only mode: no webhook secret configured → 403."""
    app.state.settings = make_settings(
        gitlab_webhook_secret=None, gitlab_poll=True, gitlab_projects="group/project"
    )
    resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
    assert resp.status_code == 403
    assert "not configured" in resp.json()["detail"]


async def test_webhook_ignores_non_mr_event(client: AsyncClient) -> None:
    resp = await client.post("/webhook", json={"object_kind": "push"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


async def test_webhook_ignores_unhandled_action(client: AsyncClient) -> None:
    payload = make_mr_payload(action="merge")
    resp = await client.post("/webhook", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.parametrize(
    ("action", "extra"),
    [("open", {}), ("update", {"oldrev": "prev_sha"})],
)
async def test_webhook_queues_handled_actions(
    client: AsyncClient, action: str, extra: dict[str, str]
) -> None:
    payload = make_mr_payload(action=action, **extra)
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
        # Third argument should be the executor, fourth should be repo_locks
        from gitlab_copilot_agent.main import app

        assert args[3] is app.state.repo_locks


# -- Deduplication tests --


async def test_webhook_skips_duplicate_head_sha(client: AsyncClient) -> None:
    """Second webhook with same project/MR/SHA is skipped."""
    from gitlab_copilot_agent.main import app

    # Pre-mark this SHA as reviewed
    app.state.review_tracker.mark(PROJECT_ID, MR_IID, "abc123")

    resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"status": "skipped", "reason": "already reviewed"}


async def test_webhook_queues_new_head_sha(client: AsyncClient) -> None:
    """New SHA on same MR is NOT skipped."""
    from gitlab_copilot_agent.main import app

    app.state.review_tracker.mark(PROJECT_ID, MR_IID, "old_sha")

    payload = make_mr_payload(last_commit={"id": "new_sha", "message": "new commit"})
    resp = await client.post("/webhook", json=payload, headers=HEADERS)
    assert resp.json() == {"status": "queued"}


async def test_webhook_marks_sha_after_successful_review(client: AsyncClient) -> None:
    """SHA is marked as reviewed only after handle_review succeeds."""
    from gitlab_copilot_agent.main import app

    tracker = app.state.review_tracker
    assert not tracker.is_reviewed(PROJECT_ID, MR_IID, "abc123")

    with patch("gitlab_copilot_agent.webhook.handle_review", new_callable=AsyncMock):
        resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.json() == {"status": "queued"}
        await asyncio.sleep(0.1)  # let background task complete

    assert tracker.is_reviewed(PROJECT_ID, MR_IID, "abc123")


async def test_webhook_does_not_mark_sha_on_review_failure(client: AsyncClient) -> None:
    """SHA is NOT marked if handle_review raises."""
    from gitlab_copilot_agent.main import app

    tracker = app.state.review_tracker

    with patch(
        "gitlab_copilot_agent.webhook.handle_review",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.json() == {"status": "queued"}
        await asyncio.sleep(0.1)

    assert not tracker.is_reviewed(PROJECT_ID, MR_IID, "abc123")


async def test_webhook_ignores_title_only_update(client: AsyncClient) -> None:
    """Update events without oldrev (title/description change only) are ignored."""
    payload = make_mr_payload(action="update")
    # No oldrev → title/description-only change
    resp = await client.post("/webhook", json=payload, headers=HEADERS)
    assert resp.json() == {"status": "ignored", "reason": "no new commits"}


async def test_webhook_queues_update_with_new_commits(client: AsyncClient) -> None:
    """Update events WITH oldrev (new commits pushed) are queued."""
    payload = make_mr_payload(action="update", oldrev="previous_sha_value")
    resp = await client.post("/webhook", json=payload, headers=HEADERS)
    assert resp.json() == {"status": "queued"}


# -- Allowlist tests --


async def test_webhook_accepts_when_allowlist_is_none(client: AsyncClient) -> None:
    """Backward compat: no allowlist configured means all projects pass."""
    assert app.state.allowed_project_ids is None
    resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
    assert resp.json()["status"] == "queued"


async def test_webhook_accepts_when_project_in_allowlist(client: AsyncClient) -> None:
    """Events from allowed projects are processed normally."""
    app.state.allowed_project_ids = {PROJECT_ID}
    try:
        resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.json()["status"] == "queued"
    finally:
        app.state.allowed_project_ids = None


async def test_webhook_rejects_when_project_not_in_allowlist(client: AsyncClient) -> None:
    """Events from non-allowed projects are ignored."""
    app.state.allowed_project_ids = {NON_ALLOWED_PROJECT_ID}
    try:
        resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.json() == {"status": "ignored", "reason": "project not in allowlist"}
    finally:
        app.state.allowed_project_ids = None
