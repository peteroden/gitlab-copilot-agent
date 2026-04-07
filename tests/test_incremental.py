"""Tests for the incremental review SHA marker module."""

from gitlab_copilot_agent.discussion_models import (
    AgentIdentity,
    Discussion,
    DiscussionHistory,
    DiscussionNote,
)
from gitlab_copilot_agent.incremental import (
    _SHA_MARKER_RE,
    extract_last_reviewed_sha,
    format_sha_marker,
)

# -- Constants --

AGENT_USER_ID = 99
AGENT_USERNAME = "review-bot"
HUMAN_USER_ID = 1
HUMAN_USERNAME = "developer"
SHA_FIRST = "aaa1111"
SHA_SECOND = "bbb2222"
TIMESTAMP_EARLY = "2026-04-06T10:00:00Z"
TIMESTAMP_LATE = "2026-04-06T12:00:00Z"

_AGENT = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)


def _make_note(
    *,
    note_id: int = 1,
    author_id: int = AGENT_USER_ID,
    body: str = "Summary note",
    created_at: str = TIMESTAMP_EARLY,
) -> DiscussionNote:
    return DiscussionNote(
        note_id=note_id,
        author_id=author_id,
        author_username=AGENT_USERNAME if author_id == AGENT_USER_ID else HUMAN_USERNAME,
        body=body,
        created_at=created_at,
        is_system=False,
        resolved=None,
        resolvable=False,
        position=None,
    )


def _make_discussion(
    discussion_id: str = "disc-1",
    notes: list[DiscussionNote] | None = None,
    is_inline: bool = False,
    is_resolved: bool = False,
) -> Discussion:
    return Discussion(
        discussion_id=discussion_id,
        notes=notes or [],
        is_inline=is_inline,
        is_resolved=is_resolved,
    )


def _make_history(discussions: list[Discussion] | None = None) -> DiscussionHistory:
    return DiscussionHistory(discussions=discussions or [], agent=_AGENT)


# -- extract_last_reviewed_sha tests --


def test_extract_sha_marker_found() -> None:
    """Overview note with agent marker returns the SHA."""
    marker_body = f"## Summary\n\n{format_sha_marker(SHA_FIRST)}"
    note = _make_note(body=marker_body)
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    assert extract_last_reviewed_sha(history) == SHA_FIRST


def test_extract_sha_marker_not_found() -> None:
    """Overview note without marker returns None."""
    note = _make_note(body="Just a summary, no marker")
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    assert extract_last_reviewed_sha(history) is None


def test_extract_sha_marker_empty_history() -> None:
    """None discussion history returns None."""
    assert extract_last_reviewed_sha(None) is None


def test_extract_sha_marker_skips_inline() -> None:
    """Marker in an inline (DiffNote) discussion is skipped."""
    marker_body = f"Inline comment\n{format_sha_marker(SHA_FIRST)}"
    note = _make_note(body=marker_body)
    disc = _make_discussion(notes=[note], is_inline=True)
    history = _make_history(discussions=[disc])

    assert extract_last_reviewed_sha(history) is None


def test_extract_sha_marker_skips_non_agent() -> None:
    """Marker in a human-authored note is skipped."""
    marker_body = f"Human note\n{format_sha_marker(SHA_FIRST)}"
    note = _make_note(author_id=HUMAN_USER_ID, body=marker_body)
    disc = _make_discussion(notes=[note])
    history = _make_history(discussions=[disc])

    assert extract_last_reviewed_sha(history) is None


def test_extract_sha_marker_multiple_picks_latest() -> None:
    """Two overview notes with markers — returns the most recent one."""
    early_body = f"First review\n{format_sha_marker(SHA_FIRST)}"
    late_body = f"Second review\n{format_sha_marker(SHA_SECOND)}"
    note_early = _make_note(note_id=1, body=early_body, created_at=TIMESTAMP_EARLY)
    note_late = _make_note(note_id=2, body=late_body, created_at=TIMESTAMP_LATE)
    disc_early = _make_discussion(discussion_id="disc-early", notes=[note_early])
    disc_late = _make_discussion(discussion_id="disc-late", notes=[note_late])
    # disc_late is later in the list → reversed() finds it first
    history = _make_history(discussions=[disc_early, disc_late])

    assert extract_last_reviewed_sha(history) == SHA_SECOND


def test_format_sha_marker_roundtrip() -> None:
    """format_sha_marker output is matched by the internal regex."""
    marker = format_sha_marker(SHA_FIRST)
    match = _SHA_MARKER_RE.search(marker)

    assert match is not None
    assert match.group(1) == SHA_FIRST
