"""Tests for the webhook endpoint."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from gitlab_copilot_agent.discussion_models import AgentIdentity
from gitlab_copilot_agent.main import app
from gitlab_copilot_agent.project_registry import ProjectRegistry, ResolvedProject
from tests.conftest import (
    GITLAB_TOKEN,
    GITLAB_URL,
    HEADERS,
    MR_IID,
    MR_PAYLOAD,
    PROJECT_ID,
    make_mr_payload,
    make_settings,
)

NON_ALLOWED_PROJECT_ID = 999

# Per-project credential constants
PER_PROJECT_TOKEN = "project-specific-token"


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


# -- Per-project credential tests --


def _make_project_registry(project_id: int = PROJECT_ID) -> ProjectRegistry:
    return ProjectRegistry(
        [
            ResolvedProject(
                jira_project="PROJ",
                repo="group/project",
                gitlab_project_id=project_id,
                clone_url=f"{GITLAB_URL}/group/project.git",
                target_branch="main",
                credential_ref="default",
                token=PER_PROJECT_TOKEN,
            )
        ]
    )


async def test_webhook_review_uses_per_project_token(client: AsyncClient) -> None:
    """MR review resolves per-project token from registry."""
    app.state.project_registry = _make_project_registry()
    mock_handle = AsyncMock()
    try:
        with patch("gitlab_copilot_agent.webhook.handle_review", mock_handle):
            resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
            assert resp.json() == {"status": "queued"}
            await asyncio.sleep(0.1)

        mock_handle.assert_awaited_once()
        _, kwargs = mock_handle.call_args
        assert kwargs["project_token"] == PER_PROJECT_TOKEN
    finally:
        app.state.project_registry = None


async def test_webhook_review_falls_back_to_global_token(client: AsyncClient) -> None:
    """MR review falls back to global token when project not in registry."""
    app.state.project_registry = _make_project_registry(project_id=9999)
    mock_handle = AsyncMock()
    try:
        with patch("gitlab_copilot_agent.webhook.handle_review", mock_handle):
            resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
            assert resp.json() == {"status": "queued"}
            await asyncio.sleep(0.1)

        mock_handle.assert_awaited_once()
        _, kwargs = mock_handle.call_args
        assert kwargs["project_token"] == GITLAB_TOKEN
    finally:
        app.state.project_registry = None


async def test_webhook_copilot_uses_per_project_token(client: AsyncClient) -> None:
    """Copilot comment resolves per-project token from registry."""
    app.state.project_registry = _make_project_registry()
    mock_handle = AsyncMock()
    try:
        with patch("gitlab_copilot_agent.webhook.handle_copilot_comment", mock_handle):
            resp = await client.post("/webhook", json=_note_body(), headers=HEADERS)
            assert resp.json() == {"status": "queued"}
            await asyncio.sleep(0.1)

        mock_handle.assert_awaited_once()
        _, kwargs = mock_handle.call_args
        assert kwargs["project_token"] == PER_PROJECT_TOKEN
    finally:
        app.state.project_registry = None


async def test_webhook_review_works_without_registry(client: AsyncClient) -> None:
    """MR review works when no project registry is configured."""
    assert app.state.project_registry is None
    mock_handle = AsyncMock()
    with patch("gitlab_copilot_agent.webhook.handle_review", mock_handle):
        resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.json() == {"status": "queued"}
        await asyncio.sleep(0.1)

    mock_handle.assert_awaited_once()
    _, kwargs = mock_handle.call_args
    assert kwargs["project_token"] == GITLAB_TOKEN


async def test_webhook_review_passes_credential_registry(client: AsyncClient) -> None:
    """MR review forwards credential_registry from app.state."""
    mock_registry = MagicMock()
    app.state.credential_registry = mock_registry
    mock_handle = AsyncMock()
    try:
        with patch("gitlab_copilot_agent.webhook.handle_review", mock_handle):
            resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
            assert resp.json() == {"status": "queued"}
            await asyncio.sleep(0.1)

        mock_handle.assert_awaited_once()
        _, kwargs = mock_handle.call_args
        assert kwargs["credential_registry"] is mock_registry
    finally:
        del app.state.credential_registry


# -- Self-comment detection tests --

AGENT_USER_ID = 100
AGENT_USERNAME = "copilot-bot"
NOTE_AUTHOR_USER_ID = 1


def _make_credential_registry_mock(
    user_id: int = AGENT_USER_ID,
    username: str = AGENT_USERNAME,
    *,
    raise_on_resolve: bool = False,
) -> MagicMock:
    """Build a mock CredentialRegistry with resolve_identity behaviour."""
    mock = MagicMock()
    if raise_on_resolve:
        mock.resolve_identity = AsyncMock(side_effect=RuntimeError("network error"))
    else:
        mock.resolve_identity = AsyncMock(
            return_value=AgentIdentity(user_id=user_id, username=username)
        )
    return mock


def _note_body_with_user(
    user_id: int = NOTE_AUTHOR_USER_ID,
    username: str = "reviewer",
    note: str = "/copilot fix the bug",
) -> dict[str, object]:
    """Build a note webhook body with a specific user id and username."""
    return {
        "object_kind": "note",
        "user": {"id": user_id, "username": username},
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


async def test_self_comment_detected_via_user_id(client: AsyncClient) -> None:
    """Note from the agent (matched by user_id) is ignored."""
    app.state.credential_registry = _make_credential_registry_mock(user_id=AGENT_USER_ID)
    try:
        body = _note_body_with_user(user_id=AGENT_USER_ID, username="someone-else")
        resp = await client.post("/webhook", json=body, headers=HEADERS)
        assert resp.json() == {"status": "ignored", "reason": "self-comment"}
    finally:
        del app.state.credential_registry


async def test_self_comment_fallback_to_username(client: AsyncClient) -> None:
    """Without credential_registry, username comparison still works."""
    app.state.settings = make_settings(agent_gitlab_username=AGENT_USERNAME)
    body = _note_body_with_user(user_id=NOTE_AUTHOR_USER_ID, username=AGENT_USERNAME)
    resp = await client.post("/webhook", json=body, headers=HEADERS)
    assert resp.json() == {"status": "ignored", "reason": "self-comment"}


async def test_no_self_comment_when_ids_differ(client: AsyncClient) -> None:
    """Note from a different user proceeds to queue."""
    app.state.credential_registry = _make_credential_registry_mock(user_id=AGENT_USER_ID)
    try:
        with patch("gitlab_copilot_agent.webhook.handle_copilot_comment"):
            body = _note_body_with_user(user_id=NOTE_AUTHOR_USER_ID)
            resp = await client.post("/webhook", json=body, headers=HEADERS)
            assert resp.json() == {"status": "queued"}
    finally:
        del app.state.credential_registry


async def test_identity_resolution_failure_falls_through(client: AsyncClient) -> None:
    """When resolve_identity raises, self-check falls through to username."""
    app.state.credential_registry = _make_credential_registry_mock(raise_on_resolve=True)
    app.state.settings = make_settings(agent_gitlab_username=AGENT_USERNAME)
    try:
        body = _note_body_with_user(user_id=NOTE_AUTHOR_USER_ID, username=AGENT_USERNAME)
        resp = await client.post("/webhook", json=body, headers=HEADERS)
        # Falls through to username check which matches → self-comment
        assert resp.json() == {"status": "ignored", "reason": "self-comment"}
    finally:
        del app.state.credential_registry


async def test_identity_resolution_failure_proceeds_when_no_username_match(
    client: AsyncClient,
) -> None:
    """When resolve_identity raises and username doesn't match, note is queued."""
    app.state.credential_registry = _make_credential_registry_mock(raise_on_resolve=True)
    try:
        with patch("gitlab_copilot_agent.webhook.handle_copilot_comment"):
            body = _note_body_with_user(user_id=NOTE_AUTHOR_USER_ID, username="reviewer")
            resp = await client.post("/webhook", json=body, headers=HEADERS)
            assert resp.json() == {"status": "queued"}
    finally:
        del app.state.credential_registry
