"""Integration test — full webhook → orchestrator pipeline with mocked externals."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from gitlab_copilot_agent.gitlab_client import MRDetails
from gitlab_copilot_agent.models import (
    MergeRequestWebhookPayload,
    MRLastCommit,
    MRObjectAttributes,
    WebhookProject,
    WebhookUser,
)
from gitlab_copilot_agent.orchestrator import handle_review
from gitlab_copilot_agent.task_executor import ReviewResult
from tests.conftest import (
    DIFF_REFS,
    FAKE_REVIEW_OUTPUT,
    GITLAB_TOKEN,
    HEADERS,
    MR_IID,
    MR_PAYLOAD,
    PROJECT_ID,
    make_settings,
)


def _setup_mocks(
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
) -> MagicMock:
    """Wire up standard mocks for the orchestrator pipeline."""
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.cleanup = AsyncMock()
    mock_gl_instance.get_mr_details = AsyncMock(
        return_value=MRDetails(
            title="Add feature",
            description="Implements X",
            diff_refs=DIFF_REFS,
            changes=[],
        )
    )
    mock_run_review.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)
    return mock_gl_instance  # type: ignore[no-any-return]


@patch("gitlab_copilot_agent.orchestrator.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
@patch("gitlab_copilot_agent.orchestrator.gitlab.Gitlab")
async def test_full_pipeline(
    mock_gl_class: MagicMock,
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
    client: AsyncClient,
) -> None:
    """Verify webhook triggers the full pipeline with correct arguments."""
    mock_gl_instance = _setup_mocks(mock_client_class, mock_run_review)

    resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}

    mock_gl_instance.clone_repo.assert_awaited_once_with(
        "https://gitlab.com/group/my-project.git", "feature/x", GITLAB_TOKEN, clone_dir=None
    )

    mock_run_review.assert_awaited_once()
    review_args = mock_run_review.call_args
    assert review_args[0][2] == "/tmp/fake-repo"
    assert review_args[0][4].title == "Add feature"

    mock_post_review.assert_awaited_once()
    post_args = mock_post_review.call_args[0]
    assert post_args[1] == PROJECT_ID
    assert post_args[2] == MR_IID

    mock_gl_instance.cleanup.assert_awaited_once()


@patch("gitlab_copilot_agent.orchestrator.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
@patch("gitlab_copilot_agent.orchestrator.gitlab.Gitlab")
async def test_orchestrator_cleans_up_on_error(
    mock_gl_class: MagicMock,
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """Verify cleanup runs even when review raises."""
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.get_mr_details = AsyncMock(side_effect=RuntimeError("SDK crashed"))
    mock_gl_instance.cleanup = AsyncMock()

    payload = MergeRequestWebhookPayload(
        object_kind="merge_request",
        user=WebhookUser(id=1, username="jdoe"),
        project=WebhookProject(
            id=PROJECT_ID,
            path_with_namespace="group/my-project",
            git_http_url="https://gitlab.com/group/my-project.git",
        ),
        object_attributes=MRObjectAttributes(
            iid=MR_IID,
            title="Add feature",
            description="Implements X",
            action="open",
            source_branch="feature/x",
            target_branch="main",
            last_commit=MRLastCommit(id="abc123", message="feat: add X"),
            url="https://gitlab.com/group/my-project/-/merge_requests/7",
        ),
    )

    with pytest.raises(RuntimeError, match="SDK crashed"):
        await handle_review(make_settings(), payload, AsyncMock())

    mock_gl_instance.cleanup.assert_awaited_once()
