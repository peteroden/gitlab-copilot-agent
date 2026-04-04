"""Tests for the webhook endpoint."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from gitlab_copilot_agent.discussion_models import AgentIdentity, Discussion, DiscussionNote
from gitlab_copilot_agent.main import app
from gitlab_copilot_agent.models import NoteWebhookPayload
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

# Agent identity constants for self-comment / @mention tests
AGENT_USER_ID = 100
AGENT_USERNAME = "copilot-bot"
NOTE_AUTHOR_USER_ID = 1


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


def _note_body(note: str = f"@{AGENT_USERNAME} fix the bug") -> dict[str, object]:
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


def _setup_credential_registry() -> MagicMock:
    """Wire a credential registry into app.state and return the mock."""
    mock = _make_credential_registry_mock()
    app.state.credential_registry = mock
    return mock


async def test_note_webhook_queues_mention(client: AsyncClient) -> None:
    """Note with @mention of agent is queued for processing."""
    _setup_credential_registry()
    try:
        with patch("gitlab_copilot_agent.webhook.handle_discussion_interaction"):
            resp = await client.post("/webhook", json=_note_body(), headers=HEADERS)
        assert resp.json()["status"] == "queued"
    finally:
        del app.state.credential_registry


async def test_note_webhook_ignores_no_mention(client: AsyncClient) -> None:
    """Note without @mention is ignored."""
    _setup_credential_registry()
    try:
        resp = await client.post("/webhook", json=_note_body("just a comment"), headers=HEADERS)
        assert resp.json() == {"status": "ignored", "reason": "not directed at agent"}
    finally:
        del app.state.credential_registry


async def test_note_webhook_ignores_without_credential_registry(client: AsyncClient) -> None:
    """Note is ignored when no credential_registry is configured."""
    resp = await client.post("/webhook", json=_note_body(), headers=HEADERS)
    assert resp.json() == {"status": "ignored", "reason": "no credential registry"}


async def test_note_webhook_uses_shared_lock_manager(client: AsyncClient) -> None:
    """Verify that the discussion handler receives repo_locks from app.state."""
    _setup_credential_registry()
    mock_handle = AsyncMock()
    try:
        with patch("gitlab_copilot_agent.webhook.handle_discussion_interaction", mock_handle):
            resp = await client.post("/webhook", json=_note_body(), headers=HEADERS)
            assert resp.json()["status"] == "queued"
            await asyncio.sleep(0.1)

            mock_handle.assert_awaited_once()
            _, kwargs = mock_handle.call_args
            assert kwargs["repo_locks"] is app.state.repo_locks
    finally:
        del app.state.credential_registry


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


async def test_webhook_discussion_uses_per_project_token(client: AsyncClient) -> None:
    """Discussion handler resolves per-project token from registry."""
    app.state.project_registry = _make_project_registry()
    app.state.credential_registry = _make_credential_registry_mock()
    mock_handle = AsyncMock()
    try:
        with patch("gitlab_copilot_agent.webhook.handle_discussion_interaction", mock_handle):
            resp = await client.post("/webhook", json=_note_body(), headers=HEADERS)
            assert resp.json() == {"status": "queued"}
            await asyncio.sleep(0.1)

        mock_handle.assert_awaited_once()
        _, kwargs = mock_handle.call_args
        assert kwargs["project_token"] == PER_PROJECT_TOKEN
    finally:
        app.state.project_registry = None
        del app.state.credential_registry


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
    note: str = f"@{AGENT_USERNAME} fix the bug",
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


async def test_no_self_comment_when_ids_differ(client: AsyncClient) -> None:
    """Note from a different user with @mention proceeds to queue."""
    app.state.credential_registry = _make_credential_registry_mock(user_id=AGENT_USER_ID)
    try:
        with patch("gitlab_copilot_agent.webhook.handle_discussion_interaction"):
            body = _note_body_with_user(user_id=NOTE_AUTHOR_USER_ID)
            resp = await client.post("/webhook", json=body, headers=HEADERS)
            assert resp.json() == {"status": "queued"}
    finally:
        del app.state.credential_registry


async def test_identity_resolution_failure_returns_ignored(client: AsyncClient) -> None:
    """When resolve_identity raises, note is ignored."""
    app.state.credential_registry = _make_credential_registry_mock(raise_on_resolve=True)
    try:
        body = _note_body_with_user(user_id=NOTE_AUTHOR_USER_ID, username=AGENT_USERNAME)
        resp = await client.post("/webhook", json=body, headers=HEADERS)
        assert resp.json() == {"status": "ignored", "reason": "identity resolution failed"}
    finally:
        del app.state.credential_registry


# -- _is_agent_directed tests --


async def test_is_agent_directed_with_mention() -> None:
    """Note containing @agent_username is directed at agent."""
    from gitlab_copilot_agent.webhook import _is_agent_directed

    identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
    payload = NoteWebhookPayload.model_validate(
        _note_body_with_user(note=f"@{AGENT_USERNAME} please review this")
    )
    assert await _is_agent_directed(payload, identity, MagicMock()) is True


async def test_is_agent_directed_without_mention() -> None:
    """Note without @mention is not directed at agent."""
    from gitlab_copilot_agent.webhook import _is_agent_directed

    identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
    payload = NoteWebhookPayload.model_validate(
        _note_body_with_user(note="just a regular comment")
    )
    assert await _is_agent_directed(payload, identity, MagicMock()) is False


async def test_is_agent_directed_wrong_username() -> None:
    """Note mentioning a different user is not directed at agent."""
    from gitlab_copilot_agent.webhook import _is_agent_directed

    identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
    payload = NoteWebhookPayload.model_validate(
        _note_body_with_user(note="@other-user please review")
    )
    assert await _is_agent_directed(payload, identity, MagicMock()) is False


async def test_is_agent_directed_rejects_substring_match() -> None:
    """@copilot-bot must not match @copilot-botty (different user)."""
    from gitlab_copilot_agent.webhook import _is_agent_directed

    identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
    payload = NoteWebhookPayload.model_validate(
        _note_body_with_user(note=f"@{AGENT_USERNAME}ty please review")
    )
    assert await _is_agent_directed(payload, identity, MagicMock()) is False


async def test_is_agent_directed_rejects_email() -> None:
    """Email addresses containing @username must not trigger."""
    from gitlab_copilot_agent.webhook import _is_agent_directed

    identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
    payload = NoteWebhookPayload.model_validate(
        _note_body_with_user(note=f"contact support@{AGENT_USERNAME}.example.com")
    )
    assert await _is_agent_directed(payload, identity, MagicMock()) is False


async def test_is_agent_directed_at_end_of_line() -> None:
    """@mention at end of text (no trailing chars) is valid."""
    from gitlab_copilot_agent.webhook import _is_agent_directed

    identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
    payload = NoteWebhookPayload.model_validate(
        _note_body_with_user(note=f"please review @{AGENT_USERNAME}")
    )
    assert await _is_agent_directed(payload, identity, MagicMock()) is True


@pytest.mark.parametrize(
    ("agent_in_thread", "expected"),
    [(True, True), (False, False)],
    ids=["agent-participated", "human-only"],
)
async def test_is_agent_directed_thread_participation(
    agent_in_thread: bool,
    expected: bool,
) -> None:
    """Thread participation: agent in thread → True, human-only → False."""
    from gitlab_copilot_agent.webhook import _is_agent_directed

    identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
    body = _note_body_with_user(note="follow-up without mention")
    body["object_attributes"]["discussion_id"] = "disc-100"
    payload = NoteWebhookPayload.model_validate(body)
    request = MagicMock()
    request.app.state.settings = make_settings()
    request.app.state.project_registry = None
    author_id = AGENT_USER_ID if agent_in_thread else NOTE_AUTHOR_USER_ID
    author_name = AGENT_USERNAME if agent_in_thread else "reviewer"
    mock_gl = AsyncMock()
    mock_gl.list_mr_discussions.return_value = [
        Discussion(
            discussion_id="disc-100",
            notes=[
                DiscussionNote(
                    note_id=10,
                    author_id=author_id,
                    author_username=author_name,
                    body="prior comment",
                    created_at="2024-01-01T00:00:00Z",
                    is_system=False,
                ),
            ],
        ),
    ]
    with patch("gitlab_copilot_agent.webhook.GitLabClient", return_value=mock_gl):
        assert await _is_agent_directed(payload, identity, request) is expected
