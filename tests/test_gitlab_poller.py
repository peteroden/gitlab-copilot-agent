"""Tests for GitLab MR poller."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.concurrency import MemoryDedup
from gitlab_copilot_agent.gitlab_poller import GitLabPoller
from tests.conftest import GITLAB_URL, MR_IID, PROJECT_ID, make_settings

# -- Constants --
MR_SHA = "deadbeef1234"
PATH_WITH_NS = "group/my-project"
_HANDLE = "gitlab_copilot_agent.gitlab_poller.handle_review"


def _mr_dict(**overrides: object) -> dict:
    base: dict = {
        "iid": MR_IID,
        "title": "Add feature",
        "description": "desc",
        "sha": MR_SHA,
        "source_branch": "feat/x",
        "target_branch": "main",
        "web_url": f"{GITLAB_URL}/{PATH_WITH_NS}/-/merge_requests/{MR_IID}",
        "author": {"id": 99, "username": "dev"},
        "references": {"full": f"{PATH_WITH_NS}/-/merge_requests/{MR_IID}"},
    }
    base.update(overrides)
    return base


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
    cl.list_project_mrs.return_value = [_mr_dict()]
    await poller._poll_once()
    mock_hr.assert_called_once()
    assert mock_hr.call_args[0][1].object_attributes.iid == MR_IID
    assert mock_hr.call_args[0][1].project.git_http_url == f"{GITLAB_URL}/{PATH_WITH_NS}.git"


@pytest.mark.asyncio
@patch(_HANDLE, new_callable=AsyncMock)
async def test_dedup_skips_seen(mock_hr: AsyncMock) -> None:
    poller, cl, _ = _poller()
    cl.list_project_mrs.return_value = [_mr_dict()]
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
