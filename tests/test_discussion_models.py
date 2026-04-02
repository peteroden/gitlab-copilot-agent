"""Unit tests for discussion_models."""

from gitlab_copilot_agent.discussion_models import (
    AgentIdentity,
    Discussion,
    DiscussionHistory,
    DiscussionNote,
)

# -- Test constants --
AGENT_USER_ID = 100
AGENT_USERNAME = "review-bot"
HUMAN_USER_ID = 42
HUMAN_USERNAME = "developer"
DISCUSSION_ID = "abc123def"
NOTE_ID = 501
CREATED_AT = "2024-01-15T10:30:00Z"
NOTE_BODY = "Consider adding a null check here."
DIFF_POSITION = {
    "new_path": "src/app.py",
    "old_path": "src/app.py",
    "new_line": 42,
    "old_line": None,
}


def _make_note(**overrides: object) -> DiscussionNote:
    """Factory for DiscussionNote with sensible defaults."""
    defaults: dict[str, object] = {
        "note_id": NOTE_ID,
        "author_id": AGENT_USER_ID,
        "author_username": AGENT_USERNAME,
        "body": NOTE_BODY,
        "created_at": CREATED_AT,
        "is_system": False,
        "resolved": None,
        "resolvable": True,
        "position": None,
    }
    return DiscussionNote(**(defaults | overrides))


def _make_discussion(**overrides: object) -> Discussion:
    """Factory for Discussion with sensible defaults."""
    defaults: dict[str, object] = {
        "discussion_id": DISCUSSION_ID,
        "notes": [_make_note()],
        "is_resolved": False,
        "is_inline": False,
    }
    return Discussion(**(defaults | overrides))


class TestDiscussionNote:
    def test_construction(self) -> None:
        note = _make_note()
        assert note.note_id == NOTE_ID
        assert note.author_id == AGENT_USER_ID
        assert note.body == NOTE_BODY
        assert note.is_system is False

    def test_with_position(self) -> None:
        note = _make_note(position=DIFF_POSITION)
        assert note.position is not None
        assert note.position["new_path"] == "src/app.py"
        assert note.position["new_line"] == 42

    def test_system_note(self) -> None:
        note = _make_note(is_system=True, body="assigned to @dev")
        assert note.is_system is True

    def test_resolved_states(self) -> None:
        unresolvable = _make_note(resolvable=False, resolved=None)
        assert unresolvable.resolved is None

        resolved = _make_note(resolvable=True, resolved=True)
        assert resolved.resolved is True

        unresolved = _make_note(resolvable=True, resolved=False)
        assert unresolved.resolved is False


class TestDiscussion:
    def test_overview_discussion(self) -> None:
        disc = _make_discussion(is_inline=False)
        assert disc.is_inline is False
        assert disc.discussion_id == DISCUSSION_ID

    def test_inline_discussion(self) -> None:
        note = _make_note(position=DIFF_POSITION)
        disc = _make_discussion(is_inline=True, notes=[note])
        assert disc.is_inline is True

    def test_resolved_discussion(self) -> None:
        disc = _make_discussion(is_resolved=True)
        assert disc.is_resolved is True

    def test_multiple_notes_in_thread(self) -> None:
        notes = [
            _make_note(note_id=1, author_id=AGENT_USER_ID, body="Issue found"),
            _make_note(note_id=2, author_id=HUMAN_USER_ID, body="Fixed"),
        ]
        disc = _make_discussion(notes=notes)
        assert len(disc.notes) == 2
        assert disc.notes[0].author_id == AGENT_USER_ID
        assert disc.notes[1].author_id == HUMAN_USER_ID


class TestAgentIdentity:
    def test_construction(self) -> None:
        identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
        assert identity.user_id == AGENT_USER_ID
        assert identity.username == AGENT_USERNAME


class TestDiscussionHistory:
    def test_empty_history(self) -> None:
        identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
        history = DiscussionHistory(discussions=[], agent=identity)
        assert len(history.discussions) == 0

    def test_with_discussions(self) -> None:
        identity = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
        discussions = [_make_discussion(), _make_discussion(discussion_id="other")]
        history = DiscussionHistory(discussions=discussions, agent=identity)
        assert len(history.discussions) == 2
        assert history.agent.user_id == AGENT_USER_ID
