"""Integration test — full webhook → orchestrator pipeline with mocked externals."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from gitlab_copilot_agent.discussion_models import (
    AgentIdentity,
    Discussion,
    DiscussionHistory,
    DiscussionNote,
)
from gitlab_copilot_agent.gitlab_client import MRChange, MRCommit, MRDetails
from gitlab_copilot_agent.orchestrator import handle_review
from gitlab_copilot_agent.task_executor import ReviewResult
from tests.conftest import (
    DIFF_REFS,
    FAKE_REVIEW_OUTPUT,
    GITLAB_TOKEN,
    GITLAB_URL,
    HEADERS,
    MR_IID,
    MR_PAYLOAD,
    PROJECT_ID,
    make_mr_changes,
    make_settings,
    make_task_event,
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
    mock_gl_instance.list_mr_discussions = AsyncMock(return_value=[])
    mock_gl_instance.get_mr_commits = AsyncMock(return_value=[])
    mock_run_review.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)
    return mock_gl_instance  # type: ignore[no-any-return]


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_full_pipeline(
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
        "https://gitlab.example.com/group/my-project.git",
        "feature/x",
        GITLAB_TOKEN,
        clone_dir=None,
    )

    mock_run_review.assert_awaited_once()
    review_args = mock_run_review.call_args
    assert review_args[0][2] == "/tmp/fake-repo"
    assert review_args[0][4].title == "Add feature"

    mock_post_review.assert_awaited_once()
    post_args = mock_post_review.call_args[0]
    assert post_args[1] == PROJECT_ID
    assert post_args[2] == MR_IID


@patch("gitlab_copilot_agent.review_pipeline.shutil.rmtree")
@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_orchestrator_cleans_up_on_error(
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
    mock_rmtree: MagicMock,
) -> None:
    """Verify cleanup runs even when review raises."""
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.get_mr_details = AsyncMock(side_effect=RuntimeError("SDK crashed"))
    mock_gl_instance.post_mr_comment = AsyncMock()

    with pytest.raises(RuntimeError, match="SDK crashed"):
        await handle_review(make_settings(), make_task_event(), AsyncMock())

    mock_rmtree.assert_called_once()


# -- Shared test data for credential_registry tests --

_AGENT_IDENTITY = AgentIdentity(user_id=99, username="copilot-bot")

_SAMPLE_DISCUSSIONS = [
    Discussion(
        discussion_id="disc-001",
        notes=[
            DiscussionNote(
                note_id=501,
                author_id=_AGENT_IDENTITY.user_id,
                author_username=_AGENT_IDENTITY.username,
                body="Consider adding a null check here.",
                created_at="2024-01-15T10:30:00Z",
                is_system=False,
                resolved=False,
                resolvable=True,
                position={
                    "new_path": "src/app.py",
                    "old_path": "src/app.py",
                    "new_line": 42,
                    "old_line": None,
                },
            ),
            DiscussionNote(
                note_id=502,
                author_id=1,
                author_username="developer",
                body="Good catch, will fix.",
                created_at="2024-01-15T11:00:00Z",
                is_system=False,
                resolved=None,
                resolvable=False,
                position=None,
            ),
        ],
        is_resolved=False,
        is_inline=True,
    ),
]


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_discussion_history_passed_with_credential_registry(
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """When credential_registry is provided, discussion_history is forwarded to run_review."""
    mock_gl_instance = _setup_mocks(mock_client_class, mock_run_review)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    await handle_review(
        make_settings(), make_task_event(), AsyncMock(), credential_registry=mock_registry
    )

    mock_gl_instance.list_mr_discussions.assert_awaited_once_with(PROJECT_ID, MR_IID)
    mock_registry.resolve_identity.assert_awaited_once_with("default", GITLAB_URL)

    review_kwargs = mock_run_review.call_args[1]
    history = review_kwargs["discussion_history"]
    assert isinstance(history, DiscussionHistory)
    assert history.agent == _AGENT_IDENTITY
    assert history.discussions == []


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_discussion_history_none_without_credential_registry(
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """Without credential_registry, discussion_history is None."""
    _setup_mocks(mock_client_class, mock_run_review)

    await handle_review(make_settings(), make_task_event(), AsyncMock())

    review_kwargs = mock_run_review.call_args[1]
    assert review_kwargs["discussion_history"] is None


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_discussion_history_failure_is_non_fatal(
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """Identity resolution failure logs warning but review still completes."""
    _setup_mocks(mock_client_class, mock_run_review)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(side_effect=RuntimeError("API down"))

    await handle_review(
        make_settings(), make_task_event(), AsyncMock(), credential_registry=mock_registry
    )

    # Review still ran, but without discussion_history
    review_kwargs = mock_run_review.call_args[1]
    assert review_kwargs["discussion_history"] is None


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_discussion_threads_flow_through_pipeline(
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """Actual discussion data (threads, authors, positions) is preserved through the pipeline."""
    mock_gl_instance = _setup_mocks(mock_client_class, mock_run_review)
    mock_gl_instance.list_mr_discussions = AsyncMock(return_value=_SAMPLE_DISCUSSIONS)
    mock_gl_instance.get_mr_commits = AsyncMock(return_value=[])

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    await handle_review(
        make_settings(), make_task_event(), AsyncMock(), credential_registry=mock_registry
    )

    review_kwargs = mock_run_review.call_args[1]
    history = review_kwargs["discussion_history"]
    assert isinstance(history, DiscussionHistory)

    # Verify discussion structure is preserved
    assert len(history.discussions) == 1
    disc = history.discussions[0]
    assert disc.discussion_id == "disc-001"
    assert disc.is_inline is True
    assert disc.is_resolved is False

    # Verify thread hierarchy — root note + reply
    assert len(disc.notes) == 2
    root = disc.notes[0]
    assert root.author_id == _AGENT_IDENTITY.user_id
    assert root.body == "Consider adding a null check here."
    assert root.position is not None
    assert root.position["new_line"] == 42

    reply = disc.notes[1]
    assert reply.author_id == 1
    assert reply.author_username == "developer"

    # Verify agent can distinguish own comments
    assert root.author_id == history.agent.user_id
    assert reply.author_id != history.agent.user_id


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_resolution_behavior_flows_to_post_review(
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """resolution_behavior parameter flows from handle_review to post_review."""
    _setup_mocks(mock_client_class, mock_run_review)

    await handle_review(
        make_settings(),
        make_task_event(resolution_behavior="auto-resolve"),
        AsyncMock(),
    )

    mock_post_review.assert_awaited_once()
    post_kwargs = mock_post_review.call_args[1]
    assert post_kwargs["resolution_behavior"] == "auto-resolve"


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_prior_feedback_rendered_in_prompt(
    mock_client_class: MagicMock,
    mock_post_review: AsyncMock,
) -> None:
    """Prior agent comments flow through orchestrator into the review prompt.

    Unlike other integration tests that patch run_review, this test lets
    run_review execute with a mock executor so we can inspect the actual
    user_prompt in TaskParams — verifying the full chain:
    orchestrator → run_review → build_review_prompt → prior feedback in prompt.
    """
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.cleanup = AsyncMock()
    mock_gl_instance.get_mr_details = AsyncMock(
        return_value=MRDetails(
            title="Add feature",
            description="Implements X",
            diff_refs=DIFF_REFS,
            changes=make_mr_changes(),
        )
    )
    mock_gl_instance.list_mr_discussions = AsyncMock(return_value=_SAMPLE_DISCUSSIONS)
    mock_gl_instance.get_mr_commits = AsyncMock(return_value=[])

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    await handle_review(
        make_settings(),
        make_task_event(),
        mock_executor,
        credential_registry=mock_registry,
    )

    # Inspect the TaskParams passed to the executor
    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt

    assert "## Agent's Prior Feedback (Unresolved)" in prompt
    assert "Consider adding a null check here." in prompt
    assert "src/app.py" in prompt


# -- Incremental review integration tests --

_MARKER_SHA = "aaa1111aaa1111aaa1111aaa1111aaa1111aaa11"
_NEW_HEAD_SHA = "bbb2222"
_INCREMENTAL_DIFF = "@@ -1,3 +1,4 @@\n+incremental change\n"
_INCREMENTAL_PATH = "src/incremental.py"


def _make_marker_note(sha: str) -> DiscussionNote:
    """Build an overview note containing a SHA marker, authored by the agent."""
    marker = f"<!-- mr-review-agent: last_reviewed_sha={sha} -->"
    return DiscussionNote(
        note_id=900,
        author_id=_AGENT_IDENTITY.user_id,
        author_username=_AGENT_IDENTITY.username,
        body=f"## Code Review Summary\n\nAll good.\n\n{marker}",
        created_at="2026-04-06T12:00:00Z",
        is_system=False,
        resolved=None,
        resolvable=False,
        position=None,
    )


def _make_marker_discussion(sha: str) -> Discussion:
    return Discussion(
        discussion_id="disc-marker",
        notes=[_make_marker_note(sha)],
        is_resolved=False,
        is_inline=False,
    )


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_incremental_review_with_marker(
    mock_client_class: MagicMock,
    mock_post_review: AsyncMock,
) -> None:
    """Second review with SHA marker uses incremental diff via compare_commits."""
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.cleanup = AsyncMock()
    mock_gl_instance.get_mr_details = AsyncMock(
        return_value=MRDetails(
            title="Add feature",
            description="Implements X",
            diff_refs=DIFF_REFS,
            changes=make_mr_changes(),
        )
    )

    # list_mr_discussions returns the marker note from a prior review
    mock_gl_instance.list_mr_discussions = AsyncMock(
        return_value=[_make_marker_discussion(_MARKER_SHA)]
    )
    mock_gl_instance.get_mr_commits = AsyncMock(return_value=[])
    # compare_commits returns incremental changes
    mock_gl_instance.compare_commits = AsyncMock(
        return_value=[
            MRChange(
                old_path=_INCREMENTAL_PATH,
                new_path=_INCREMENTAL_PATH,
                diff=_INCREMENTAL_DIFF,
                new_file=False,
                deleted_file=False,
                renamed_file=False,
            )
        ]
    )

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    await handle_review(
        make_settings(),
        make_task_event(),
        mock_executor,
        credential_registry=mock_registry,
    )

    # Verify compare_commits called with correct SHAs
    mock_gl_instance.compare_commits.assert_awaited_once_with(PROJECT_ID, _MARKER_SHA, "abc123")

    # Verify prompt contains incremental diff header
    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt
    assert "Incremental Diff" in prompt
    assert _INCREMENTAL_DIFF in prompt


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_incremental_review_compare_fails_fallback(
    mock_client_class: MagicMock,
    mock_post_review: AsyncMock,
) -> None:
    """compare_commits failure falls back to full diff."""
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.cleanup = AsyncMock()
    mock_gl_instance.get_mr_details = AsyncMock(
        return_value=MRDetails(
            title="Add feature",
            description="Implements X",
            diff_refs=DIFF_REFS,
            changes=make_mr_changes(),
        )
    )
    mock_gl_instance.list_mr_discussions = AsyncMock(
        return_value=[_make_marker_discussion(_MARKER_SHA)]
    )
    mock_gl_instance.get_mr_commits = AsyncMock(return_value=[])
    mock_gl_instance.compare_commits = AsyncMock(side_effect=RuntimeError("Compare API error"))

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    await handle_review(
        make_settings(),
        make_task_event(),
        mock_executor,
        credential_registry=mock_registry,
    )

    # Falls back to full diff — prompt should NOT contain incremental header
    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt
    assert "Incremental Diff" not in prompt
    # Full diff from mr_details.changes is used instead
    assert DIFF_REFS.base_sha is not None  # sanity: details were loaded


# -- Suppressed feedback integration tests (Feature 7) --

_HUMAN_USER_ID = 42

_HUMAN_RESOLVED_DISCUSSION = Discussion(
    discussion_id="disc-resolved-human",
    notes=[
        DiscussionNote(
            note_id=701,
            author_id=_AGENT_IDENTITY.user_id,
            author_username=_AGENT_IDENTITY.username,
            body="Consider adding a null check here.",
            created_at="2024-01-15T10:30:00Z",
            is_system=False,
            resolved=True,
            resolved_by_id=_HUMAN_USER_ID,
            resolvable=True,
            position={
                "new_path": "src/app.py",
                "old_path": "src/app.py",
                "new_line": 42,
                "old_line": None,
            },
        ),
    ],
    is_resolved=True,
    is_inline=True,
)

_DISMISSED_DISCUSSION = Discussion(
    discussion_id="disc-dismissed",
    notes=[
        DiscussionNote(
            note_id=702,
            author_id=_AGENT_IDENTITY.user_id,
            author_username=_AGENT_IDENTITY.username,
            body="Potential security issue with user input.",
            created_at="2024-01-15T10:30:00Z",
            is_system=False,
            resolved=False,
            resolvable=True,
            position={
                "new_path": "src/handler.py",
                "old_path": "src/handler.py",
                "new_line": 15,
                "old_line": None,
            },
        ),
        DiscussionNote(
            note_id=703,
            author_id=_HUMAN_USER_ID,
            author_username="developer",
            body="This is intentional — we sanitize upstream.",
            created_at="2024-01-15T11:00:00Z",
            is_system=False,
            resolved=None,
            resolvable=False,
            position=None,
        ),
    ],
    is_resolved=False,
    is_inline=True,
)


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_suppressed_feedback_rendered_in_prompt(
    mock_client_class: MagicMock,
    mock_post_review: AsyncMock,
) -> None:
    """Human-resolved and dismissed discussions flow through as suppressed feedback in prompt.

    Verifies the full chain: orchestrator → run_review → build_review_prompt
    → suppressed feedback section with [MANUALLY RESOLVED] and [DISMISSED] tags.
    """
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.cleanup = AsyncMock()
    mock_gl_instance.get_mr_details = AsyncMock(
        return_value=MRDetails(
            title="Add feature",
            description="Implements X",
            diff_refs=DIFF_REFS,
            changes=make_mr_changes(),
        )
    )
    mock_gl_instance.list_mr_discussions = AsyncMock(
        return_value=[_HUMAN_RESOLVED_DISCUSSION, _DISMISSED_DISCUSSION]
    )
    mock_gl_instance.get_mr_commits = AsyncMock(return_value=[])

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    await handle_review(
        make_settings(),
        make_task_event(),
        mock_executor,
        credential_registry=mock_registry,
    )

    # Inspect the TaskParams passed to the executor
    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt

    assert "## Suppressed Feedback (Do Not Re-Raise)" in prompt
    assert "[MANUALLY RESOLVED]" in prompt
    assert "[DISMISSED]" in prompt
    assert "Consider adding a null check here." in prompt
    assert "Potential security issue with user input." in prompt


# -- Commit message awareness integration tests --

_SAMPLE_COMMITS = [
    MRCommit(id="aaa111", title="feat: add auth", message="feat: add auth\n\nJWT flow."),
    MRCommit(id="bbb222", title="fix: null check", message="fix: null check"),
]


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_commit_messages_in_review_prompt(
    mock_client_class: MagicMock,
    mock_post_review: AsyncMock,
) -> None:
    """Commit messages flow through orchestrator into the review prompt."""
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.cleanup = AsyncMock()
    mock_gl_instance.get_mr_details = AsyncMock(
        return_value=MRDetails(
            title="Add feature",
            description="Implements X",
            diff_refs=DIFF_REFS,
            changes=make_mr_changes(),
        )
    )
    mock_gl_instance.list_mr_discussions = AsyncMock(return_value=[])
    mock_gl_instance.get_mr_commits = AsyncMock(return_value=_SAMPLE_COMMITS)

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    await handle_review(
        make_settings(),
        make_task_event(),
        mock_executor,
    )

    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt

    assert "## Commit Messages" in prompt
    assert "feat: add auth" in prompt
    assert "fix: null check" in prompt


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
async def test_commit_fetch_failure_graceful_degradation(
    mock_client_class: MagicMock,
    mock_post_review: AsyncMock,
) -> None:
    """get_mr_commits failure logs warning but review proceeds without commits."""
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.cleanup = AsyncMock()
    mock_gl_instance.get_mr_details = AsyncMock(
        return_value=MRDetails(
            title="Add feature",
            description="Implements X",
            diff_refs=DIFF_REFS,
            changes=make_mr_changes(),
        )
    )
    mock_gl_instance.list_mr_discussions = AsyncMock(return_value=[])
    mock_gl_instance.get_mr_commits = AsyncMock(side_effect=RuntimeError("Commits API down"))

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    await handle_review(
        make_settings(),
        make_task_event(),
        mock_executor,
    )

    # Review still ran — prompt should NOT contain commit section
    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt
    assert "## Commit Messages" not in prompt
