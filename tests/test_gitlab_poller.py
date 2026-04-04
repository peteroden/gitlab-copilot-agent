"""Tests for GitLab MR and note poller."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.concurrency import MemoryDedup
from gitlab_copilot_agent.credential_registry import CredentialRegistry
from gitlab_copilot_agent.discussion_models import AgentIdentity, Discussion, DiscussionNote
from gitlab_copilot_agent.gitlab_client import MRAuthor, MRListItem, NoteListItem
from gitlab_copilot_agent.gitlab_poller import GitLabPoller
from gitlab_copilot_agent.project_registry import ProjectRegistry, ResolvedProject
from gitlab_copilot_agent.task_executor import TaskExecutionError
from tests.conftest import GITLAB_URL, MR_IID, PROJECT_ID, make_settings

# -- Constants --
MR_SHA = "deadbeef1234"
PATH_WITH_NS = "group/my-project"
MR_WEB_URL = f"{GITLAB_URL}/{PATH_WITH_NS}/-/merge_requests/{MR_IID}"
MR_AUTHOR = MRAuthor(id=99, username="dev")
NOTE_AUTHOR = MRAuthor(id=42, username="reviewer")
AGENT_USERNAME = "review-bot"
AGENT_USER_ID = 100
AGENT_IDENTITY = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
NOTE_ID = 777
MENTION_BODY = f"@{AGENT_USERNAME} review this"
PER_PROJECT_TOKEN = "project-specific-token"
_HANDLE_REVIEW = "gitlab_copilot_agent.gitlab_poller.handle_review"
_HANDLE_DISCUSSION = "gitlab_copilot_agent.gitlab_poller.handle_discussion_interaction"


def _mr_item(**overrides: object) -> MRListItem:
    defaults = {
        "iid": MR_IID,
        "title": "Add feature",
        "description": "desc",
        "sha": MR_SHA,
        "source_branch": "feat/x",
        "target_branch": "main",
        "web_url": MR_WEB_URL,
        "state": "opened",
        "author": MR_AUTHOR,
        "updated_at": "2024-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return MRListItem.model_validate(defaults)


def _note_item(**overrides: object) -> NoteListItem:
    defaults = {
        "id": NOTE_ID,
        "body": MENTION_BODY,
        "author": NOTE_AUTHOR,
        "system": False,
        "created_at": "2024-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return NoteListItem.model_validate(defaults)


def _discussion(
    note_id: int = NOTE_ID,
    body: str = MENTION_BODY,
    author: MRAuthor = NOTE_AUTHOR,
    is_system: bool = False,
    is_resolved: bool = False,
    discussion_id: str | None = None,
    extra_notes: list[DiscussionNote] | None = None,
) -> Discussion:
    """Create a Discussion with a single DiscussionNote for testing."""
    disc_id = discussion_id or f"disc-{note_id}"
    main_note = DiscussionNote(
        note_id=note_id,
        author_id=author.id,
        author_username=author.username,
        body=body,
        created_at="2024-01-01T00:00:00Z",
        is_system=is_system,
    )
    notes = [*(extra_notes or []), main_note]
    return Discussion(discussion_id=disc_id, notes=notes, is_resolved=is_resolved)


def _mock_credential_registry() -> AsyncMock:
    """Create a mock CredentialRegistry that resolves to the test agent identity."""
    registry = AsyncMock(spec=CredentialRegistry)
    registry.resolve_identity.return_value = AGENT_IDENTITY
    return registry


def _poller(
    client: AsyncMock | None = None,
    dedup: MemoryDedup | None = None,
    credential_registry: AsyncMock | None = None,
) -> tuple[GitLabPoller, AsyncMock, MemoryDedup]:
    cl = client or AsyncMock()
    # Default: no discussions unless overridden
    cl.list_mr_discussions.return_value = []
    dd = dedup or MemoryDedup()
    creds = credential_registry or _mock_credential_registry()
    p = GitLabPoller(cl, make_settings(), {PROJECT_ID}, dd, AsyncMock(), credential_registry=creds)
    return p, cl, dd


@pytest.mark.asyncio
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_poll_once_discovers_mr(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    await poller._poll_once()
    mock_hr.assert_called_once()
    assert mock_hr.call_args[0][1].object_attributes.iid == MR_IID
    assert mock_hr.call_args[0][1].project.git_http_url == f"{GITLAB_URL}/{PATH_WITH_NS}.git"


@pytest.mark.asyncio
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_dedup_skips_seen(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    await poller._poll_once()
    await poller._poll_once()
    assert mock_hr.call_count == 1


@pytest.mark.asyncio
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_task_execution_failure_marks_review_seen(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    mock_hr.side_effect = TaskExecutionError("runner error")

    await poller._poll_once()
    await poller._poll_once()

    assert mock_hr.call_count == 1


@pytest.mark.asyncio
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_task_execution_failure_does_not_abort_other_reviews(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item(iid=1), _mr_item(iid=2, sha="beadfeed")]
    mock_hr.side_effect = [TaskExecutionError("runner error"), None]

    await poller._poll_once()

    assert mock_hr.call_count == 2


@pytest.mark.asyncio
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_watermark_advances(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = []
    assert poller._watermark is None
    await poller._poll_once()
    assert poller._watermark is not None


@pytest.mark.asyncio
async def test_per_project_error_is_logged_not_raised() -> None:
    """A failing project logs the error with credential_ref but doesn't crash the poll."""
    poller, cl, _ = _poller()
    cl.list_project_mrs.side_effect = RuntimeError("403 Forbidden")

    # Should NOT raise — error is caught per-project
    await poller._poll_once()

    # Watermarks still advance (poll completed)
    assert poller._watermark is not None


@pytest.mark.asyncio
async def test_start_stop_lifecycle() -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = []
    await poller.start()
    assert poller._task is not None
    assert not poller._task.done()
    await poller.stop()
    assert poller._task.done()


@pytest.mark.asyncio
async def test_stop_is_noop_when_not_started() -> None:
    poller, _, _ = _poller()
    await poller.stop()  # should not raise


@pytest.mark.asyncio
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_poll_loop_resets_failures_on_success(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    call_count = 0

    async def _fail_then_succeed(pid: int, **kwargs: object) -> list[MRListItem]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return []

    cl.list_project_mrs.side_effect = _fail_then_succeed
    poller._interval = 0  # no delay in test
    await poller.start()
    # Let the loop run a few iterations
    await asyncio.sleep(0.15)
    await poller.stop()
    assert poller._failures == 0  # reset after success


# -- Note discovery tests --


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_note_discovery_triggers_discussion_orchestrator(
    mock_hr: AsyncMock, mock_hd: AsyncMock
) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    cl.list_mr_discussions.return_value = [_discussion()]
    await poller._poll_once()
    mock_hd.assert_called_once()
    payload = mock_hd.call_args[0][1]
    assert payload.object_attributes.note == MENTION_BODY
    assert payload.merge_request.iid == MR_IID


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_note_skips_system_notes(mock_hr: AsyncMock, mock_hd: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    cl.list_mr_discussions.return_value = [_discussion(is_system=True)]
    await poller._poll_once()
    mock_hd.assert_not_called()


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_note_skips_non_mention_comments(mock_hr: AsyncMock, mock_hd: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    cl.list_mr_discussions.return_value = [_discussion(body="just a regular comment")]
    await poller._poll_once()
    mock_hd.assert_not_called()


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_note_dedup_skips_seen(mock_hr: AsyncMock, mock_hd: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    cl.list_mr_discussions.return_value = [_discussion()]
    await poller._poll_once()
    await poller._poll_once()
    assert mock_hd.call_count == 1


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_task_execution_failure_marks_note_seen(
    mock_hr: AsyncMock, mock_hd: AsyncMock
) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    cl.list_mr_discussions.return_value = [_discussion()]
    mock_hd.side_effect = TaskExecutionError("runner error")

    await poller._poll_once()
    await poller._poll_once()

    assert mock_hd.call_count == 1


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_task_execution_failure_does_not_abort_other_notes(
    mock_hr: AsyncMock, mock_hd: AsyncMock
) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    cl.list_mr_discussions.return_value = [_discussion(note_id=1), _discussion(note_id=2)]
    mock_hd.side_effect = [TaskExecutionError("runner error"), None]

    await poller._poll_once()

    assert mock_hd.call_count == 2


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_note_payload_has_correct_project_info(
    mock_hr: AsyncMock, mock_hd: AsyncMock
) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    cl.list_mr_discussions.return_value = [_discussion()]
    await poller._poll_once()
    payload = mock_hd.call_args[0][1]
    assert payload.project.path_with_namespace == PATH_WITH_NS
    assert payload.project.git_http_url == f"{GITLAB_URL}/{PATH_WITH_NS}.git"
    assert payload.user.username == NOTE_AUTHOR.username


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_note_skips_self_authored_comments(mock_hr: AsyncMock, mock_hd: AsyncMock) -> None:
    """Agent's own @mention notes are ignored (consistent with webhook)."""
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    agent_author = MRAuthor(id=AGENT_USER_ID, username=AGENT_USERNAME)
    cl.list_mr_discussions.return_value = [_discussion(author=agent_author)]
    await poller._poll_once()
    mock_hd.assert_not_called()


@pytest.mark.asyncio
async def test_start_initializes_watermark() -> None:
    """Watermark set to 'now' on start to avoid replaying history."""
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = []
    assert poller._watermark is None
    await poller.start()
    assert poller._watermark is not None
    await poller.stop()


# -- Per-project credential tests --


def _make_project_registry(project_id: int = PROJECT_ID) -> ProjectRegistry:
    return ProjectRegistry(
        [
            ResolvedProject(
                jira_project="PROJ",
                repo=PATH_WITH_NS,
                gitlab_project_id=project_id,
                clone_url=f"{GITLAB_URL}/{PATH_WITH_NS}.git",
                target_branch="main",
                credential_ref="default",
                token=PER_PROJECT_TOKEN,
            )
        ]
    )


@pytest.mark.asyncio
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_poll_passes_per_project_token_to_review(mock_hr: AsyncMock) -> None:
    """Poller passes per-project token from registry to handle_review."""
    registry = _make_project_registry()
    poller, cl, _ = _poller()
    poller._project_registry = registry
    # Pre-populate client cache so no real GitLabClient is created
    poller._project_clients["default"] = cl
    cl.list_project_mrs.return_value = [_mr_item()]
    await poller._poll_once()
    mock_hr.assert_called_once()
    _, kwargs = mock_hr.call_args
    assert kwargs["project_token"] == PER_PROJECT_TOKEN


@pytest.mark.asyncio
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_poll_falls_back_to_none_when_not_in_registry(mock_hr: AsyncMock) -> None:
    """Poller passes None token when project not in registry (global fallback)."""
    registry = _make_project_registry(project_id=9999)
    poller, cl, _ = _poller()
    poller._project_registry = registry
    cl.list_project_mrs.return_value = [_mr_item()]
    await poller._poll_once()
    mock_hr.assert_called_once()
    _, kwargs = mock_hr.call_args
    assert kwargs["project_token"] is None


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_poll_passes_per_project_token_to_discussion_orchestrator(
    mock_hr: AsyncMock, mock_hd: AsyncMock
) -> None:
    """Poller passes per-project token from registry to handle_discussion_interaction."""
    registry = _make_project_registry()
    poller, cl, _ = _poller()
    poller._project_registry = registry
    poller._project_clients["default"] = cl
    cl.list_project_mrs.return_value = [_mr_item()]
    cl.list_mr_discussions.return_value = [_discussion()]
    await poller._poll_once()
    mock_hd.assert_called_once()
    _, kwargs = mock_hd.call_args
    assert kwargs["project_token"] == PER_PROJECT_TOKEN


@pytest.mark.asyncio
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_poll_uses_per_project_client_for_discovery(mock_hr: AsyncMock) -> None:
    """Poller uses per-project client for MR discovery, not the default client."""
    registry = _make_project_registry()
    poller, default_cl, _ = _poller()
    poller._project_registry = registry
    project_cl = AsyncMock()
    project_cl.list_project_mrs.return_value = [_mr_item()]
    project_cl.list_mr_discussions.return_value = []
    poller._project_clients["default"] = project_cl
    await poller._poll_once()
    # Per-project client used for discovery, not the default
    project_cl.list_project_mrs.assert_called_once()
    default_cl.list_project_mrs.assert_not_called()


# -- Thread participation tests --


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_note_agent_participated_thread_triggers_handler(
    mock_hr: AsyncMock, mock_hd: AsyncMock
) -> None:
    """Reply in thread with prior agent note (no @mention) triggers handler."""
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    agent_note = DiscussionNote(
        note_id=10,
        author_id=AGENT_USER_ID,
        author_username=AGENT_USERNAME,
        body="agent review",
        created_at="2024-01-01T00:00:00Z",
        is_system=False,
    )
    cl.list_mr_discussions.return_value = [
        _discussion(body="follow-up without mention", extra_notes=[agent_note]),
    ]
    await poller._poll_once()
    mock_hd.assert_called_once()


@pytest.mark.asyncio
@patch(_HANDLE_DISCUSSION, new_callable=AsyncMock)
@patch(_HANDLE_REVIEW, new_callable=AsyncMock)
async def test_note_human_only_thread_ignored(mock_hr: AsyncMock, mock_hd: AsyncMock) -> None:
    """Reply in thread with no agent notes and no @mention is ignored."""
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    cl.list_mr_discussions.return_value = [
        _discussion(body="just a human follow-up"),
    ]
    await poller._poll_once()
    mock_hd.assert_not_called()
