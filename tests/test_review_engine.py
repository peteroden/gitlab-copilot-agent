"""Tests for the review engine prompt construction and run_review."""

from unittest.mock import AsyncMock

from gitlab_copilot_agent.discussion_models import (
    AgentIdentity,
    Discussion,
    DiscussionHistory,
    DiscussionNote,
)
from gitlab_copilot_agent.prompt_defaults import get_prompt
from gitlab_copilot_agent.review_engine import (
    ReviewRequest,
    _format_prior_feedback,
    build_review_prompt,
    run_review,
)
from tests.conftest import EXAMPLE_CLONE_URL, make_settings

# -- Agent note & discussion helpers for prior-feedback tests --
AGENT_USER_ID = 100
AGENT_USERNAME = "copilot-bot"


def _make_agent_note(
    note_id: int = 1,
    author_id: int = AGENT_USER_ID,
    body: str = "**[WARNING]** Consider error handling",
    resolved: bool | None = False,
    file_path: str = "src/main.py",
    line: int = 42,
) -> DiscussionNote:
    return DiscussionNote(
        note_id=note_id,
        author_id=author_id,
        author_username="bot",
        body=body,
        created_at="2026-04-06T12:00:00Z",
        is_system=False,
        resolved=resolved,
        resolvable=True,
        position={
            "new_path": file_path,
            "new_line": line,
            "old_path": file_path,
            "old_line": None,
        },
    )


def _make_discussion(
    discussion_id: str = "disc-1",
    notes: list[DiscussionNote] | None = None,
    is_resolved: bool = False,
    is_inline: bool = True,
) -> Discussion:
    return Discussion(
        discussion_id=discussion_id,
        notes=notes or [],
        is_resolved=is_resolved,
        is_inline=is_inline,
    )


def _make_history(
    discussions: list[Discussion] | None = None,
    agent_user_id: int = AGENT_USER_ID,
) -> DiscussionHistory:
    return DiscussionHistory(
        discussions=discussions or [],
        agent=AgentIdentity(user_id=agent_user_id, username=AGENT_USERNAME),
    )


def _make_request() -> ReviewRequest:
    return ReviewRequest(
        title="Add feature X",
        description="Implements feature X",
        source_branch="feature/x",
        target_branch="main",
    )


def test_build_review_prompt_constructs_git_diff_command() -> None:
    prompt = build_review_prompt(_make_request())
    assert "git diff main...feature/x" in prompt
    assert "Add feature X" in prompt
    assert "Implements feature X" in prompt


def test_build_review_prompt_handles_no_description() -> None:
    req = ReviewRequest(
        title="No desc",
        description=None,
        source_branch="feat",
        target_branch="main",
    )
    prompt = build_review_prompt(req)
    assert "(none)" in prompt


async def test_run_review_delegates_to_executor() -> None:
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "Review result"

    settings = make_settings()
    req = _make_request()
    result = await run_review(mock_executor, settings, "/tmp/repo", EXAMPLE_CLONE_URL, req)

    assert result == "Review result"
    task = mock_executor.execute.call_args[0][0]
    assert task.system_prompt == get_prompt(settings, "review")
    assert "Add feature X" in task.user_prompt
    assert "git diff main...feature/x" in task.user_prompt


def test_build_review_prompt_includes_prior_feedback() -> None:
    """Unresolved agent comments appear in the prompt as prior feedback."""
    note = _make_agent_note(body="**[WARNING]** Consider error handling")
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    prompt = build_review_prompt(
        _make_request(), diff_text="some diff", discussion_history=history
    )

    assert "## Agent's Prior Feedback (Unresolved)" in prompt
    assert "Consider error handling" in prompt
    assert "Review ONLY" in prompt


async def test_run_review_forwards_discussion_history() -> None:
    """run_review passes discussion_history through to build_review_prompt."""
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "Review result"

    settings = make_settings()
    req = _make_request()
    history = DiscussionHistory(
        discussions=[],
        agent=AgentIdentity(user_id=1, username="bot"),
    )
    result = await run_review(
        mock_executor,
        settings,
        "/tmp/repo",
        EXAMPLE_CLONE_URL,
        req,
        discussion_history=history,
    )

    assert result == "Review result"
    # Verify executor was called (prompt content tested separately)
    mock_executor.execute.assert_awaited_once()


# -- _format_prior_feedback unit tests --


def test_format_prior_feedback_groups_by_file() -> None:
    """Comments are grouped by file and sorted by line number."""
    notes = [
        _make_agent_note(note_id=1, body="Issue A", file_path="src/b.py", line=10),
        _make_agent_note(note_id=2, body="Issue B", file_path="src/a.py", line=5),
        _make_agent_note(note_id=3, body="Issue C", file_path="src/a.py", line=1),
    ]
    discussions = [_make_discussion(discussion_id=f"d{i}", notes=[n]) for i, n in enumerate(notes)]
    history = _make_history(discussions=discussions)

    result = _format_prior_feedback(history)

    # File a.py appears before b.py (sorted)
    assert result.index("src/a.py") < result.index("src/b.py")
    # Within a.py, line 1 appears before line 5
    assert result.index("Line 1") < result.index("Line 5")
    assert "Issue A" in result
    assert "Issue B" in result
    assert "Issue C" in result


def test_format_prior_feedback_excludes_resolved() -> None:
    """Resolved discussions produce no output."""
    note = _make_agent_note()
    disc = _make_discussion(notes=[note], is_resolved=True)
    history = _make_history(discussions=[disc])

    assert _format_prior_feedback(history) == ""


def test_format_prior_feedback_excludes_human_comments() -> None:
    """Discussions authored by humans (non-agent) produce no output."""
    note = _make_agent_note(author_id=999)
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    assert _format_prior_feedback(history) == ""


def test_format_prior_feedback_excludes_overview_notes() -> None:
    """Non-inline (overview) discussions produce no output."""
    note = _make_agent_note()
    disc = _make_discussion(notes=[note], is_inline=False)
    history = _make_history(discussions=[disc])

    assert _format_prior_feedback(history) == ""


def test_format_prior_feedback_strips_severity_prefix() -> None:
    """Severity prefixes like **[WARNING]** are removed from output."""
    note = _make_agent_note(body="**[WARNING]** Consider error handling")
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    result = _format_prior_feedback(history)

    assert "Consider error handling" in result
    assert "**[WARNING]**" not in result


def test_format_prior_feedback_strips_suggestion_blocks() -> None:
    """Suggestion code blocks are removed from output."""
    body = "Fix the return type\n\n```suggestion:-0+0\nreturn None\n```"
    note = _make_agent_note(body=body)
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    result = _format_prior_feedback(history)

    assert "Fix the return type" in result
    assert "```suggestion" not in result
    assert "return None" not in result


def test_format_prior_feedback_empty_history() -> None:
    """Empty discussions list produces no output."""
    history = _make_history(discussions=[])

    assert _format_prior_feedback(history) == ""


# -- build_review_prompt integration tests --


def test_build_review_prompt_omits_prior_feedback_when_none() -> None:
    """No prior feedback section when discussion_history is None."""
    prompt = build_review_prompt(_make_request(), diff_text="some diff")

    assert "Prior Feedback" not in prompt
    assert "Review ONLY" in prompt


def test_build_review_prompt_omits_prior_feedback_when_all_resolved() -> None:
    """No prior feedback section when all discussions are resolved."""
    note = _make_agent_note()
    disc = _make_discussion(notes=[note], is_resolved=True)
    history = _make_history(discussions=[disc])

    prompt = build_review_prompt(
        _make_request(), diff_text="some diff", discussion_history=history
    )

    assert "Prior Feedback" not in prompt
    assert "Review ONLY" in prompt


def test_build_review_prompt_includes_prior_feedback_without_diff() -> None:
    """Prior feedback renders even when diff_text is not provided."""
    note = _make_agent_note(body="**[WARNING]** Consider error handling")
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    prompt = build_review_prompt(_make_request(), discussion_history=history)

    assert "## Agent's Prior Feedback (Unresolved)" in prompt
    assert "Consider error handling" in prompt
    assert "git diff" in prompt


def test_format_prior_feedback_skips_none_new_path() -> None:
    """Notes with new_path=None are excluded (not rendered as 'None')."""
    note = _make_agent_note()
    # Override position to have None new_path
    note_dict = note.model_dump()
    note_dict["position"]["new_path"] = None
    patched_note = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched_note])
    history = _make_history(discussions=[disc])

    assert _format_prior_feedback(history) == ""


def test_format_prior_feedback_handles_non_numeric_line() -> None:
    """Non-numeric new_line degrades to 'General', not a crash."""
    note = _make_agent_note()
    note_dict = note.model_dump()
    note_dict["position"]["new_line"] = "not-a-number"
    patched_note = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched_note])
    history = _make_history(discussions=[disc])

    result = _format_prior_feedback(history)

    assert "General:" in result
    assert "Consider error handling" in result


def test_format_prior_feedback_skips_null_position() -> None:
    """Notes with position=None are excluded entirely."""
    note = DiscussionNote(
        note_id=1,
        author_id=AGENT_USER_ID,
        author_username="bot",
        body="**[WARNING]** Some issue",
        created_at="2026-04-06T12:00:00Z",
        is_system=False,
        resolved=False,
        resolvable=True,
        position=None,
    )
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    assert _format_prior_feedback(history) == ""


# -- Discussion ID and resolution eval instruction tests --


def test_prior_feedback_includes_discussion_id() -> None:
    """Prior feedback lines contain [discussion: ...] tag."""
    note = _make_agent_note(body="**[WARNING]** Consider error handling")
    disc = _make_discussion(discussion_id="disc-tag-test", notes=[note])
    history = _make_history(discussions=[disc])

    result = _format_prior_feedback(history)

    assert "[discussion: disc-tag-test]" in result
    assert "Consider error handling" in result


def test_resolution_eval_instructions_present_when_prior_feedback() -> None:
    """Resolution evaluation instructions appended when prior feedback exists."""
    note = _make_agent_note(body="**[WARNING]** Consider error handling")
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    prompt = build_review_prompt(
        _make_request(), diff_text="some diff", discussion_history=history
    )

    assert "## Resolution Evaluation" in prompt
    assert "resolved|not_addressed|partial" in prompt
    assert "discussion_id" in prompt


def test_no_resolution_eval_instructions_without_prior_feedback() -> None:
    """No resolution evaluation instructions when no prior feedback exists."""
    # No discussion history at all
    prompt = build_review_prompt(_make_request(), diff_text="some diff")
    assert "Resolution Evaluation" not in prompt

    # Empty discussions
    history = _make_history(discussions=[])
    prompt = build_review_prompt(
        _make_request(), diff_text="some diff", discussion_history=history
    )
    assert "Resolution Evaluation" not in prompt

    # All resolved discussions
    note = _make_agent_note()
    disc = _make_discussion(notes=[note], is_resolved=True)
    history = _make_history(discussions=[disc])
    prompt = build_review_prompt(
        _make_request(), diff_text="some diff", discussion_history=history
    )
    assert "Resolution Evaluation" not in prompt
