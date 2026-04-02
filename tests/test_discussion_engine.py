"""Tests for discussion_engine — prompt construction and response parsing."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from gitlab_copilot_agent.discussion_engine import (
    DiscussionResponse,
    build_discussion_prompt,
    parse_discussion_response,
    run_discussion,
)
from gitlab_copilot_agent.discussion_models import (
    AgentIdentity,
    Discussion,
    DiscussionHistory,
    DiscussionNote,
)
from gitlab_copilot_agent.gitlab_client import MRChange, MRDetails, MRDiffRef
from tests.conftest import EXAMPLE_CLONE_URL, make_settings

# -- Test constants --
AGENT_USER_ID = 99
AGENT_USERNAME = "review-bot"
HUMAN_USER_ID = 42
HUMAN_USERNAME = "developer"
DISCUSSION_ID = "disc-001"
MR_TITLE = "Add user authentication"
MR_DESCRIPTION = "Implements JWT-based auth"
NOTE_BODY_AGENT = "Consider adding input validation here."
NOTE_BODY_HUMAN = "Why is this important?"
SOURCE_BRANCH = "feature/auth"
SYSTEM_PROMPT = "You are a helpful discussion assistant."

_DIFF_REFS = MRDiffRef(base_sha="aaa", start_sha="bbb", head_sha="ccc")

_SAMPLE_DIFF = "@@ -1,3 +1,5 @@\n+import jwt\n+\n def login():\n     pass"


# -- Factories --


def _make_agent() -> AgentIdentity:
    return AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)


def _make_note(
    author_id: int = AGENT_USER_ID,
    author_username: str = AGENT_USERNAME,
    body: str = NOTE_BODY_AGENT,
    **kwargs: Any,
) -> DiscussionNote:
    defaults: dict[str, Any] = {
        "note_id": 1,
        "created_at": "2024-01-15T10:00:00Z",
        "is_system": False,
        "resolved": None,
        "resolvable": True,
        "position": None,
    }
    return DiscussionNote(
        author_id=author_id, author_username=author_username, body=body, **(defaults | kwargs)
    )


def _make_discussion(
    discussion_id: str = DISCUSSION_ID,
    notes: list[DiscussionNote] | None = None,
    **kwargs: Any,
) -> Discussion:
    defaults: dict[str, Any] = {"is_resolved": False, "is_inline": True}
    return Discussion(
        discussion_id=discussion_id,
        notes=notes or [_make_note()],
        **(defaults | kwargs),
    )


def _make_mr_details(**overrides: Any) -> MRDetails:
    defaults: dict[str, Any] = {
        "title": MR_TITLE,
        "description": MR_DESCRIPTION,
        "diff_refs": _DIFF_REFS,
        "changes": [
            MRChange(
                old_path="src/auth.py",
                new_path="src/auth.py",
                diff=_SAMPLE_DIFF,
                new_file=False,
                deleted_file=False,
                renamed_file=False,
            )
        ],
    }
    return MRDetails(**(defaults | overrides))


# -- Prompt construction tests --


class TestBuildDiscussionPrompt:
    def test_includes_mr_metadata(self) -> None:
        agent = _make_agent()
        disc = _make_discussion()
        history = DiscussionHistory(discussions=[disc], agent=agent)
        mr = _make_mr_details()
        prompt = build_discussion_prompt(mr, history, disc)
        assert MR_TITLE in prompt
        assert MR_DESCRIPTION in prompt

    def test_includes_thread_with_roles(self) -> None:
        agent = _make_agent()
        notes = [
            _make_note(body=NOTE_BODY_AGENT),
            _make_note(
                author_id=HUMAN_USER_ID,
                author_username=HUMAN_USERNAME,
                body=NOTE_BODY_HUMAN,
            ),
        ]
        disc = _make_discussion(notes=notes)
        history = DiscussionHistory(discussions=[disc], agent=agent)
        prompt = build_discussion_prompt(_make_mr_details(), history, disc)
        assert "**Agent**" in prompt
        assert f"**{HUMAN_USERNAME}**" in prompt
        assert NOTE_BODY_AGENT in prompt
        assert NOTE_BODY_HUMAN in prompt

    def test_includes_diff(self) -> None:
        agent = _make_agent()
        disc = _make_discussion()
        history = DiscussionHistory(discussions=[disc], agent=agent)
        prompt = build_discussion_prompt(_make_mr_details(), history, disc)
        assert "import jwt" in prompt

    def test_includes_other_discussions(self) -> None:
        agent = _make_agent()
        trigger = _make_discussion(discussion_id="trigger")
        other = _make_discussion(
            discussion_id="other",
            notes=[_make_note(body="Some other issue")],
        )
        history = DiscussionHistory(discussions=[trigger, other], agent=agent)
        prompt = build_discussion_prompt(_make_mr_details(), history, trigger)
        assert "Other Active Discussions" in prompt
        assert "Some other issue" in prompt

    def test_empty_diff(self) -> None:
        agent = _make_agent()
        disc = _make_discussion()
        history = DiscussionHistory(discussions=[disc], agent=agent)
        mr = _make_mr_details(changes=[])
        prompt = build_discussion_prompt(mr, history, disc)
        assert "Diff" not in prompt

    def test_no_description_shows_none(self) -> None:
        agent = _make_agent()
        disc = _make_discussion()
        history = DiscussionHistory(discussions=[disc], agent=agent)
        mr = _make_mr_details(description=None)
        prompt = build_discussion_prompt(mr, history, disc)
        assert "(none)" in prompt

    def test_ends_with_instruction(self) -> None:
        agent = _make_agent()
        disc = _make_discussion()
        history = DiscussionHistory(discussions=[disc], agent=agent)
        prompt = build_discussion_prompt(_make_mr_details(), history, disc)
        assert prompt.endswith("Respond to the latest message in the Current Thread above.")


# -- Response parsing tests --


class TestParseDiscussionResponse:
    def test_plain_text_reply(self) -> None:
        raw = "Here's why that matters: the input could be null."
        result = parse_discussion_response(raw)
        assert "null" in result.reply
        assert result.has_code_changes is False

    def test_coding_with_files_changed_block(self) -> None:
        raw = (
            "I've added the null check.\n\n"
            "```json\n"
            '{"summary": "Added null check", "files_changed": ["src/app.py"]}'
            "\n```"
        )
        result = parse_discussion_response(raw)
        assert result.has_code_changes is True
        assert "added the null check" in result.reply.lower()
        assert "files_changed" not in result.reply

    def test_json_without_files_changed_is_plain_reply(self) -> None:
        raw = '```json\n{"status": "ok"}\n```'
        result = parse_discussion_response(raw)
        assert result.has_code_changes is False

    def test_invalid_json_falls_back(self) -> None:
        raw = "```json\n{invalid json}\n```"
        result = parse_discussion_response(raw)
        assert result.has_code_changes is False
        assert "invalid" in result.reply.lower()

    def test_summary_fallback_when_no_text_before_block(self) -> None:
        raw = '```json\n{"summary": "Fixed it", "files_changed": ["a.py"]}\n```'
        result = parse_discussion_response(raw)
        assert result.has_code_changes is True
        assert result.reply == "Fixed it"

    def test_model_is_frozen(self) -> None:
        import pytest
        from pydantic import ValidationError

        result = DiscussionResponse(reply="test")
        with pytest.raises(ValidationError):
            result.reply = "changed"  # type: ignore[misc]


# -- run_discussion tests --


class TestRunDiscussion:
    async def test_delegates_to_executor(self) -> None:
        mock_executor = AsyncMock()
        mock_executor.execute.return_value = "Discussion result"

        settings = make_settings()
        user_prompt = "Respond to the thread."

        result = await run_discussion(
            executor=mock_executor,
            settings=settings,
            repo_path="/tmp/repo",
            repo_url=EXAMPLE_CLONE_URL,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            source_branch=SOURCE_BRANCH,
        )

        assert result == "Discussion result"
        task = mock_executor.execute.call_args[0][0]
        assert task.task_type == "coding"
        assert task.task_id == f"discussion-{SOURCE_BRANCH}"
        assert task.system_prompt == SYSTEM_PROMPT
        assert task.user_prompt == user_prompt
        assert task.repo_url == EXAMPLE_CLONE_URL
        assert task.branch == SOURCE_BRANCH
