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
    MAX_COMMIT_CHARS,
    ReviewRequest,
    _is_dismissed,
    _is_human_resolved,
    build_review_prompt,
    format_prior_feedback,
    format_suppressed_feedback,
    run_review,
)
from gitlab_copilot_agent.task_executor import ReviewResult
from tests.conftest import EXAMPLE_CLONE_URL, make_settings

# -- Agent note & discussion helpers for prior-feedback tests --
AGENT_USER_ID = 100
HUMAN_USER_ID = 42
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


def _make_request(commit_messages: list[str] | None = None) -> ReviewRequest:
    return ReviewRequest(
        title="Add feature X",
        description="Implements feature X",
        source_branch="feature/x",
        target_branch="main",
        commit_messages=commit_messages or [],
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
    expected = ReviewResult(summary="Review result")
    mock_executor.execute.return_value = expected

    settings = make_settings()
    req = _make_request()
    result = await run_review(mock_executor, settings, "/tmp/repo", EXAMPLE_CLONE_URL, req)

    assert result == expected
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
    expected = ReviewResult(summary="Review result")
    mock_executor.execute.return_value = expected

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

    assert result == expected
    # Verify executor was called (prompt content tested separately)
    mock_executor.execute.assert_awaited_once()


# -- format_prior_feedback unit tests --


def test_format_prior_feedback_groups_by_file() -> None:
    """Comments are grouped by file and sorted by line number."""
    notes = [
        _make_agent_note(note_id=1, body="Issue A", file_path="src/b.py", line=10),
        _make_agent_note(note_id=2, body="Issue B", file_path="src/a.py", line=5),
        _make_agent_note(note_id=3, body="Issue C", file_path="src/a.py", line=1),
    ]
    discussions = [_make_discussion(discussion_id=f"d{i}", notes=[n]) for i, n in enumerate(notes)]
    history = _make_history(discussions=discussions)

    result = format_prior_feedback(history)

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

    assert format_prior_feedback(history) == ""


def test_format_prior_feedback_excludes_human_comments() -> None:
    """Discussions authored by humans (non-agent) produce no output."""
    note = _make_agent_note(author_id=999)
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    assert format_prior_feedback(history) == ""


def test_format_prior_feedback_excludes_overview_notes() -> None:
    """Non-inline (overview) discussions produce no output."""
    note = _make_agent_note()
    disc = _make_discussion(notes=[note], is_inline=False)
    history = _make_history(discussions=[disc])

    assert format_prior_feedback(history) == ""


def test_format_prior_feedback_strips_severity_prefix() -> None:
    """Severity prefixes like **[WARNING]** are removed from output."""
    note = _make_agent_note(body="**[WARNING]** Consider error handling")
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    result = format_prior_feedback(history)

    assert "Consider error handling" in result
    assert "**[WARNING]**" not in result


def test_format_prior_feedback_strips_suggestion_blocks() -> None:
    """Suggestion code blocks are removed from output."""
    body = "Fix the return type\n\n```suggestion:-0+0\nreturn None\n```"
    note = _make_agent_note(body=body)
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    result = format_prior_feedback(history)

    assert "Fix the return type" in result
    assert "```suggestion" not in result
    assert "return None" not in result


def test_format_prior_feedback_empty_history() -> None:
    """Empty discussions list produces no output."""
    history = _make_history(discussions=[])

    assert format_prior_feedback(history) == ""


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

    assert format_prior_feedback(history) == ""


def test_format_prior_feedback_handles_non_numeric_line() -> None:
    """Non-numeric new_line degrades to 'General', not a crash."""
    note = _make_agent_note()
    note_dict = note.model_dump()
    note_dict["position"]["new_line"] = "not-a-number"
    patched_note = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched_note])
    history = _make_history(discussions=[disc])

    result = format_prior_feedback(history)

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

    assert format_prior_feedback(history) == ""


# -- Discussion ID and resolution eval instruction tests --


def test_prior_feedback_includes_discussion_id() -> None:
    """Prior feedback lines contain [discussion: ...] tag."""
    note = _make_agent_note(body="**[WARNING]** Consider error handling")
    disc = _make_discussion(discussion_id="disc-tag-test", notes=[note])
    history = _make_history(discussions=[disc])

    result = format_prior_feedback(history)

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


# -- Incremental diff header tests --

INCREMENTAL_HEADER = "Incremental Diff"


def test_build_prompt_incremental_header() -> None:
    """is_incremental=True adds 'Incremental Diff' header to the prompt."""
    prompt = build_review_prompt(_make_request(), diff_text="some diff", is_incremental=True)

    assert INCREMENTAL_HEADER in prompt
    assert "changes since last review" in prompt


def test_build_prompt_full_no_header() -> None:
    """is_incremental=False does NOT include 'Incremental Diff' header."""
    prompt = build_review_prompt(_make_request(), diff_text="some diff", is_incremental=False)

    assert INCREMENTAL_HEADER not in prompt


# -- Outdated position annotation tests --

OUTDATED_ANNOTATION = "(outdated position"
POSITION_HEAD_SHA = "pos_head_abc"
CURRENT_HEAD_SHA = "current_head_xyz"


def test_prior_feedback_outdated_annotation() -> None:
    """Position head_sha != current_head_sha adds '(outdated position' annotation."""
    note = _make_agent_note()
    note_dict = note.model_dump()
    note_dict["position"]["head_sha"] = POSITION_HEAD_SHA
    patched_note = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched_note])
    history = _make_history(discussions=[disc])

    result = format_prior_feedback(history, current_head_sha=CURRENT_HEAD_SHA)

    assert OUTDATED_ANNOTATION in result


def test_prior_feedback_current_no_annotation() -> None:
    """Position head_sha == current_head_sha has no outdated annotation."""
    note = _make_agent_note()
    note_dict = note.model_dump()
    note_dict["position"]["head_sha"] = CURRENT_HEAD_SHA
    patched_note = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched_note])
    history = _make_history(discussions=[disc])

    result = format_prior_feedback(history, current_head_sha=CURRENT_HEAD_SHA)

    assert OUTDATED_ANNOTATION not in result
    assert "Consider error handling" in result


# -- _is_human_resolved tests --


def test_is_human_resolved_true_when_human_resolves() -> None:
    """Returns True when discussion is resolved by a non-agent user."""
    note = _make_agent_note(resolved=True)
    note_dict = note.model_dump()
    note_dict["resolved_by_id"] = HUMAN_USER_ID
    patched = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched], is_resolved=True)

    assert _is_human_resolved(disc, AGENT_USER_ID) is True


def test_is_human_resolved_false_when_agent_resolves() -> None:
    """Returns False when discussion is resolved by the agent itself."""
    note = _make_agent_note(resolved=True)
    note_dict = note.model_dump()
    note_dict["resolved_by_id"] = AGENT_USER_ID
    patched = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched], is_resolved=True)

    assert _is_human_resolved(disc, AGENT_USER_ID) is False


def test_is_human_resolved_false_when_unresolved() -> None:
    """Returns False when discussion is not resolved."""
    note = _make_agent_note()
    disc = _make_discussion(notes=[note], is_resolved=False)

    assert _is_human_resolved(disc, AGENT_USER_ID) is False


def test_is_human_resolved_false_when_no_resolved_by_id() -> None:
    """Returns False when resolved but resolved_by_id is None on all notes."""
    note = _make_agent_note(resolved=True)
    disc = _make_discussion(notes=[note], is_resolved=True)

    assert _is_human_resolved(disc, AGENT_USER_ID) is False


# -- _is_dismissed tests --


def test_is_dismissed_matches_wont_fix() -> None:
    """Detects 'won't fix' dismissal pattern."""
    agent_note = _make_agent_note()
    human_reply = DiscussionNote(
        note_id=2,
        author_id=HUMAN_USER_ID,
        author_username="dev",
        body="Won't fix this — it's fine as is.",
        created_at="2026-04-06T13:00:00Z",
        is_system=False,
        resolved=None,
        resolvable=False,
        position=None,
    )
    disc = _make_discussion(notes=[agent_note, human_reply])

    assert _is_dismissed(disc, AGENT_USER_ID) is True


def test_is_dismissed_matches_false_positive() -> None:
    """Detects 'false positive' dismissal pattern."""
    agent_note = _make_agent_note()
    human_reply = DiscussionNote(
        note_id=2,
        author_id=HUMAN_USER_ID,
        author_username="dev",
        body="This is a false positive.",
        created_at="2026-04-06T13:00:00Z",
        is_system=False,
        resolved=None,
        resolvable=False,
        position=None,
    )
    disc = _make_discussion(notes=[agent_note, human_reply])

    assert _is_dismissed(disc, AGENT_USER_ID) is True


def test_is_dismissed_case_insensitive() -> None:
    """Dismissal detection is case-insensitive."""
    agent_note = _make_agent_note()
    human_reply = DiscussionNote(
        note_id=2,
        author_id=HUMAN_USER_ID,
        author_username="dev",
        body="BY DESIGN — this is expected behavior.",
        created_at="2026-04-06T13:00:00Z",
        is_system=False,
        resolved=None,
        resolvable=False,
        position=None,
    )
    disc = _make_discussion(notes=[agent_note, human_reply])

    assert _is_dismissed(disc, AGENT_USER_ID) is True


def test_is_dismissed_ignores_agent_notes() -> None:
    """Agent's own notes are not scanned for dismissal patterns."""
    agent_note = _make_agent_note(body="This is not a bug to fix")
    disc = _make_discussion(notes=[agent_note])

    assert _is_dismissed(disc, AGENT_USER_ID) is False


def test_is_dismissed_no_match_on_normal_reply() -> None:
    """Normal developer replies do not trigger dismissal."""
    agent_note = _make_agent_note()
    human_reply = DiscussionNote(
        note_id=2,
        author_id=HUMAN_USER_ID,
        author_username="dev",
        body="Thanks, I'll fix this in the next commit.",
        created_at="2026-04-06T13:00:00Z",
        is_system=False,
        resolved=None,
        resolvable=False,
        position=None,
    )
    disc = _make_discussion(notes=[agent_note, human_reply])

    assert _is_dismissed(disc, AGENT_USER_ID) is False


def test_is_dismissed_matches_all_patterns() -> None:
    """All dismissal patterns are recognized."""
    patterns = [
        "won't fix",
        "wontfix",
        "intentional",
        "by design",
        "not a bug",
        "false positive",
        "not an issue",
        "acceptable risk",
    ]
    for phrase in patterns:
        agent_note = _make_agent_note()
        human_reply = DiscussionNote(
            note_id=2,
            author_id=HUMAN_USER_ID,
            author_username="dev",
            body=f"Marking as {phrase}",
            created_at="2026-04-06T13:00:00Z",
            is_system=False,
            resolved=None,
            resolvable=False,
            position=None,
        )
        disc = _make_discussion(notes=[agent_note, human_reply])
        assert _is_dismissed(disc, AGENT_USER_ID) is True, f"Failed for: {phrase}"


# -- format_suppressed_feedback tests --


def test_format_suppressed_feedback_human_resolved() -> None:
    """Human-resolved inline discussions appear with [MANUALLY RESOLVED] tag."""
    note = _make_agent_note()
    note_dict = note.model_dump()
    note_dict["resolved_by_id"] = HUMAN_USER_ID
    patched = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched], is_resolved=True)
    history = _make_history(discussions=[disc])

    result = format_suppressed_feedback(history)

    assert "## Suppressed Feedback (Do Not Re-Raise)" in result
    assert "[MANUALLY RESOLVED]" in result
    assert "Consider error handling" in result


def test_format_suppressed_feedback_dismissed() -> None:
    """Dismissed inline discussions appear with [DISMISSED] tag."""
    agent_note = _make_agent_note()
    human_reply = DiscussionNote(
        note_id=2,
        author_id=HUMAN_USER_ID,
        author_username="dev",
        body="This is intentional.",
        created_at="2026-04-06T13:00:00Z",
        is_system=False,
        resolved=None,
        resolvable=False,
        position=None,
    )
    disc = _make_discussion(notes=[agent_note, human_reply], is_resolved=False)
    history = _make_history(discussions=[disc])

    result = format_suppressed_feedback(history)

    assert "## Suppressed Feedback (Do Not Re-Raise)" in result
    assert "[DISMISSED]" in result


def test_format_suppressed_feedback_empty_when_no_items() -> None:
    """Returns empty string when no discussions qualify as suppressed."""
    note = _make_agent_note()
    disc = _make_discussion(notes=[note], is_resolved=False)
    history = _make_history(discussions=[disc])

    result = format_suppressed_feedback(history)

    assert result == ""


def test_format_suppressed_feedback_skips_non_inline() -> None:
    """Non-inline (overview) discussions are excluded from suppressed feedback."""
    note = _make_agent_note()
    note_dict = note.model_dump()
    note_dict["resolved_by_id"] = HUMAN_USER_ID
    patched = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched], is_resolved=True, is_inline=False)
    history = _make_history(discussions=[disc])

    result = format_suppressed_feedback(history)

    assert result == ""


def test_format_suppressed_feedback_skips_human_authored() -> None:
    """Discussions not authored by the agent are excluded."""
    note = _make_agent_note(author_id=HUMAN_USER_ID)
    note_dict = note.model_dump()
    note_dict["resolved_by_id"] = HUMAN_USER_ID
    patched = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched], is_resolved=True)
    history = _make_history(discussions=[disc])

    result = format_suppressed_feedback(history)

    assert result == ""


def test_format_suppressed_feedback_includes_rules() -> None:
    """Suppressed feedback section includes suppression rules."""
    note = _make_agent_note()
    note_dict = note.model_dump()
    note_dict["resolved_by_id"] = HUMAN_USER_ID
    patched = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched], is_resolved=True)
    history = _make_history(discussions=[disc])

    result = format_suppressed_feedback(history)

    assert "Do NOT re-raise" in result
    assert "respect the developer's decision" in result


# -- Suppressed feedback in build_review_prompt tests --


def test_build_review_prompt_includes_suppressed_feedback() -> None:
    """Suppressed feedback section appears in the review prompt."""
    note = _make_agent_note()
    note_dict = note.model_dump()
    note_dict["resolved_by_id"] = HUMAN_USER_ID
    patched = DiscussionNote(**note_dict)
    disc = _make_discussion(notes=[patched], is_resolved=True)
    history = _make_history(discussions=[disc])

    prompt = build_review_prompt(
        _make_request(), diff_text="some diff", discussion_history=history
    )

    assert "## Suppressed Feedback (Do Not Re-Raise)" in prompt
    assert "[MANUALLY RESOLVED]" in prompt


def test_build_review_prompt_omits_suppressed_when_empty() -> None:
    """Suppressed section omitted when no discussions qualify."""
    note = _make_agent_note()
    disc = _make_discussion(notes=[note], is_resolved=False)
    history = _make_history(discussions=[disc])

    prompt = build_review_prompt(
        _make_request(), diff_text="some diff", discussion_history=history
    )

    assert "Suppressed Feedback" not in prompt


# -- Commit message prompt tests --

SAMPLE_COMMIT_MESSAGES = [
    "feat: add user authentication\n\nImplement JWT-based auth flow.",
    "fix: handle null pointer in parser",
    "chore: update dependencies",
]


def test_build_review_prompt_includes_commit_messages() -> None:
    """Commit messages appear in the prompt when provided."""
    req = _make_request(commit_messages=SAMPLE_COMMIT_MESSAGES)
    prompt = build_review_prompt(req, diff_text="some diff")

    assert "## Commit Messages" in prompt
    assert "feat: add user authentication" in prompt
    assert "fix: handle null pointer in parser" in prompt
    assert "chore: update dependencies" in prompt


def test_build_review_prompt_omits_commit_section_when_empty() -> None:
    """No commit section when commit_messages is empty."""
    prompt = build_review_prompt(_make_request(), diff_text="some diff")

    assert "## Commit Messages" not in prompt


def test_build_review_prompt_truncates_commit_messages() -> None:
    """Commit messages are truncated at MAX_COMMIT_CHARS."""
    # Create messages that exceed the limit
    long_msg = "x" * 500
    many_messages = [f"commit {i}: {long_msg}" for i in range(20)]
    req = _make_request(commit_messages=many_messages)

    prompt = build_review_prompt(req, diff_text="some diff")

    assert "## Commit Messages" in prompt
    assert "truncated" in prompt
    # The commit section should not exceed MAX_COMMIT_CHARS by much
    commit_start = prompt.index("## Commit Messages")
    # Find the end — next section header or end of string
    next_section = prompt.find("## ", commit_start + 1)
    if next_section == -1:
        next_section = len(prompt)
    commit_section = prompt[commit_start:next_section]
    # Allow for header + truncation message overhead
    assert len(commit_section) < MAX_COMMIT_CHARS + 500


def test_build_review_prompt_commit_messages_before_diff() -> None:
    """Commit messages section appears before the diff section."""
    req = _make_request(commit_messages=["feat: something"])
    prompt = build_review_prompt(req, diff_text="some diff")

    commit_pos = prompt.index("## Commit Messages")
    diff_pos = prompt.index("## Diff")
    assert commit_pos < diff_pos
