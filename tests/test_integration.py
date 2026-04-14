"""Integration test — full webhook → pipeline with mocked externals."""

from pathlib import Path
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
from gitlab_copilot_agent.pipeline import run_pipeline
from gitlab_copilot_agent.review_pipeline import ReviewContext, ReviewPipeline
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
    make_mock_gitlab_client,
    make_mr_details,
    make_settings,
    make_task_event,
)


def _make_gl_client(
    mock_run_review: AsyncMock,
    *,
    mr_details_override: MRDetails | None = None,
) -> AsyncMock:
    """Wire up a mock GitLabClient for review pipeline tests."""
    gl = make_mock_gitlab_client(
        Path("/tmp/fake-repo"),
        mr_details=mr_details_override or make_mr_details(changes=[]),
    )
    mock_run_review.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)
    return gl


def _make_pipeline(
    gl: AsyncMock,
    executor: AsyncMock | None = None,
    credential_registry: AsyncMock | None = None,
    **event_overrides: object,
) -> ReviewPipeline:
    """Build a ReviewPipeline with the given mock client."""
    return ReviewPipeline(
        settings=make_settings(),
        event=make_task_event(**event_overrides),
        executor=executor or AsyncMock(),
        gl_client=gl,
        credential_registry=credential_registry,
    )


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
async def test_full_pipeline(
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
    client: AsyncClient,
) -> None:
    """Verify webhook triggers the full pipeline with correct arguments."""
    gl = _make_gl_client(mock_run_review)
    with patch("gitlab_copilot_agent.gitlab_webhook.GitLabClient", return_value=gl):
        resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"status": "queued"}

    import asyncio

    await asyncio.sleep(0.1)

    gl.clone_repo.assert_awaited_once_with(
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
async def test_pipeline_cleans_up_on_error(
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
    mock_rmtree: MagicMock,
) -> None:
    """Verify cleanup runs even when review raises."""
    gl = make_mock_gitlab_client(Path("/tmp/fake-repo"))
    gl.get_mr_details.side_effect = RuntimeError("SDK crashed")

    with pytest.raises(RuntimeError, match="SDK crashed"):
        pipeline = _make_pipeline(gl)
        await run_pipeline(pipeline, ReviewContext())

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
async def test_discussion_history_passed_with_credential_registry(
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """When credential_registry is provided, discussion_history is forwarded to run_review."""
    gl = _make_gl_client(mock_run_review)
    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    pipeline = _make_pipeline(gl, credential_registry=mock_registry)
    await run_pipeline(pipeline, ReviewContext())

    gl.list_mr_discussions.assert_awaited_once_with(PROJECT_ID, MR_IID)
    mock_registry.resolve_identity.assert_awaited_once_with("default", GITLAB_URL)

    review_kwargs = mock_run_review.call_args[1]
    history = review_kwargs["discussion_history"]
    assert isinstance(history, DiscussionHistory)
    assert history.agent == _AGENT_IDENTITY
    assert history.discussions == []


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
async def test_discussion_history_none_without_credential_registry(
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """Without credential_registry, discussion_history is None."""
    gl = _make_gl_client(mock_run_review)

    pipeline = _make_pipeline(gl)
    await run_pipeline(pipeline, ReviewContext())

    review_kwargs = mock_run_review.call_args[1]
    assert review_kwargs["discussion_history"] is None


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
async def test_discussion_history_failure_is_non_fatal(
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """Identity resolution failure logs warning but review still completes."""
    gl = _make_gl_client(mock_run_review)
    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(side_effect=RuntimeError("API down"))

    pipeline = _make_pipeline(gl, credential_registry=mock_registry)
    await run_pipeline(pipeline, ReviewContext())

    # Review still ran, but without discussion_history
    review_kwargs = mock_run_review.call_args[1]
    assert review_kwargs["discussion_history"] is None


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
async def test_discussion_threads_flow_through_pipeline(
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """Actual discussion data (threads, authors, positions) is preserved through the pipeline."""
    gl = _make_gl_client(mock_run_review)
    gl.list_mr_discussions.return_value = _SAMPLE_DISCUSSIONS

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    pipeline = _make_pipeline(gl, credential_registry=mock_registry)
    await run_pipeline(pipeline, ReviewContext())

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
async def test_resolution_behavior_flows_to_post_review(
    mock_run_review: AsyncMock,
    mock_post_review: AsyncMock,
) -> None:
    """resolution_behavior parameter flows from pipeline to post_review."""
    gl = _make_gl_client(mock_run_review)

    pipeline = _make_pipeline(gl, resolution_behavior="auto-resolve")
    await run_pipeline(pipeline, ReviewContext())

    mock_post_review.assert_awaited_once()
    post_kwargs = mock_post_review.call_args[1]
    assert post_kwargs["resolution_behavior"] == "auto-resolve"


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
async def test_prior_feedback_rendered_in_prompt(
    mock_post_review: AsyncMock,
) -> None:
    """Prior agent comments flow through pipeline into the review prompt."""
    gl = make_mock_gitlab_client(
        Path("/tmp/fake-repo"),
        mr_details=make_mr_details(),
        discussions=_SAMPLE_DISCUSSIONS,
    )

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    pipeline = _make_pipeline(gl, executor=mock_executor, credential_registry=mock_registry)
    await run_pipeline(pipeline, ReviewContext())

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
async def test_incremental_review_with_marker(
    mock_post_review: AsyncMock,
) -> None:
    """Second review with SHA marker uses incremental diff via compare_commits."""
    gl = make_mock_gitlab_client(
        Path("/tmp/fake-repo"),
        mr_details=make_mr_details(),
        discussions=[_make_marker_discussion(_MARKER_SHA)],
    )
    gl.compare_commits.return_value = [
        MRChange(
            old_path=_INCREMENTAL_PATH,
            new_path=_INCREMENTAL_PATH,
            diff=_INCREMENTAL_DIFF,
            new_file=False,
            deleted_file=False,
            renamed_file=False,
        )
    ]

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    pipeline = _make_pipeline(gl, executor=mock_executor, credential_registry=mock_registry)
    await run_pipeline(pipeline, ReviewContext())

    gl.compare_commits.assert_awaited_once_with(PROJECT_ID, _MARKER_SHA, "abc123")

    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt
    assert "Incremental Diff" in prompt
    assert _INCREMENTAL_DIFF in prompt


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
async def test_incremental_review_compare_fails_fallback(
    mock_post_review: AsyncMock,
) -> None:
    """compare_commits failure falls back to full diff."""
    gl = make_mock_gitlab_client(
        Path("/tmp/fake-repo"),
        mr_details=make_mr_details(),
        discussions=[_make_marker_discussion(_MARKER_SHA)],
    )
    gl.compare_commits.side_effect = RuntimeError("Compare API error")

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    pipeline = _make_pipeline(gl, executor=mock_executor, credential_registry=mock_registry)
    await run_pipeline(pipeline, ReviewContext())

    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt
    assert "Incremental Diff" not in prompt
    assert DIFF_REFS.base_sha is not None


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
async def test_suppressed_feedback_rendered_in_prompt(
    mock_post_review: AsyncMock,
) -> None:
    """Human-resolved and dismissed discussions flow through as suppressed feedback in prompt."""
    gl = make_mock_gitlab_client(
        Path("/tmp/fake-repo"),
        mr_details=make_mr_details(),
        discussions=[_HUMAN_RESOLVED_DISCUSSION, _DISMISSED_DISCUSSION],
    )

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    mock_registry = AsyncMock()
    mock_registry.resolve_identity = AsyncMock(return_value=_AGENT_IDENTITY)

    pipeline = _make_pipeline(gl, executor=mock_executor, credential_registry=mock_registry)
    await run_pipeline(pipeline, ReviewContext())

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
async def test_commit_messages_in_review_prompt(
    mock_post_review: AsyncMock,
) -> None:
    """Commit messages flow through pipeline into the review prompt."""
    gl = make_mock_gitlab_client(
        Path("/tmp/fake-repo"),
        mr_details=make_mr_details(),
        commits=_SAMPLE_COMMITS,
    )

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    pipeline = _make_pipeline(gl, executor=mock_executor)
    await run_pipeline(pipeline, ReviewContext())

    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt

    assert "## Commit Messages" in prompt
    assert "feat: add auth" in prompt
    assert "fix: null check" in prompt


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
async def test_commit_fetch_failure_graceful_degradation(
    mock_post_review: AsyncMock,
) -> None:
    """get_mr_commits failure logs warning but review proceeds without commits."""
    gl = make_mock_gitlab_client(
        Path("/tmp/fake-repo"),
        mr_details=make_mr_details(),
    )
    gl.get_mr_commits.side_effect = RuntimeError("Commits API down")

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    pipeline = _make_pipeline(gl, executor=mock_executor)
    await run_pipeline(pipeline, ReviewContext())

    task_params = mock_executor.execute.call_args[0][0]
    prompt = task_params.user_prompt
    assert "## Commit Messages" not in prompt
