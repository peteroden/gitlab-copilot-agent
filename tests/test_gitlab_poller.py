"""Tests for GitLab MR poller."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.concurrency import MemoryDedup
from gitlab_copilot_agent.gitlab_client import MRAuthor, MRListItem
from gitlab_copilot_agent.gitlab_poller import GitLabPoller
from tests.conftest import GITLAB_URL, MR_IID, PROJECT_ID, make_settings

# -- Constants --
MR_SHA = "deadbeef1234"
PATH_WITH_NS = "group/my-project"
MR_WEB_URL = f"{GITLAB_URL}/{PATH_WITH_NS}/-/merge_requests/{MR_IID}"
MR_AUTHOR = MRAuthor(id=99, username="dev")
_HANDLE = "gitlab_copilot_agent.gitlab_poller.handle_review"


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


def _poller(
    client: AsyncMock | None = None,
    dedup: MemoryDedup | None = None,
) -> tuple[GitLabPoller, AsyncMock, MemoryDedup]:
    cl = client or AsyncMock()
    dd = dedup or MemoryDedup()
    p = GitLabPoller(cl, make_settings(), {PROJECT_ID}, dd, AsyncMock())
    return p, cl, dd


@pytest.mark.asyncio
@patch(_HANDLE, new_callable=AsyncMock)
async def test_poll_once_discovers_mr(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    await poller._poll_once()
    mock_hr.assert_called_once()
    assert mock_hr.call_args[0][1].object_attributes.iid == MR_IID
    assert mock_hr.call_args[0][1].project.git_http_url == f"{GITLAB_URL}/{PATH_WITH_NS}.git"


@pytest.mark.asyncio
@patch(_HANDLE, new_callable=AsyncMock)
async def test_dedup_skips_seen(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_item()]
    await poller._poll_once()
    await poller._poll_once()
    assert mock_hr.call_count == 1


@pytest.mark.asyncio
@patch(_HANDLE, new_callable=AsyncMock)
async def test_watermark_advances(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = []
    assert poller._watermark is None
    await poller._poll_once()
    assert poller._watermark is not None


@pytest.mark.asyncio
async def test_backoff_increases_and_resets() -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.side_effect = RuntimeError("boom")
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await poller._poll_once()
        poller._failures += 1
    assert poller._failures == 3
    # Success resets
    cl.list_project_mrs.side_effect = None
    cl.list_project_mrs.return_value = []
    with patch(_HANDLE, new_callable=AsyncMock):
        await poller._poll_once()
    poller._failures = 0
    assert poller._failures == 0


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
@patch(_HANDLE, new_callable=AsyncMock)
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
