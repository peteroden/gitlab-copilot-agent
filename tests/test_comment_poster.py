"""Tests for comment posting to GitLab."""

from unittest.mock import MagicMock

from gitlab_copilot_agent.comment_parser import ParsedReview, ReviewComment
from gitlab_copilot_agent.comment_poster import post_review
from tests.conftest import DIFF_REFS, MR_IID, PROJECT_ID


async def test_posts_inline_and_summary() -> None:
    gl = MagicMock()
    review = ParsedReview(
        comments=[ReviewComment(file="src/main.py", line=10, severity="error", comment="Bug")],
        summary="Needs fixes.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review)

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    assert mr.discussions.create.call_count == 1
    assert mr.notes.create.call_count == 1
    assert "Needs fixes" in mr.notes.create.call_args[0][0]["body"]


async def test_posts_summary_only_when_no_comments() -> None:
    gl = MagicMock()
    review = ParsedReview(comments=[], summary="All good.")
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review)

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    assert mr.discussions.create.call_count == 0
    assert mr.notes.create.call_count == 1


async def test_inline_failure_falls_back_to_note() -> None:
    gl = MagicMock()
    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    mr.discussions.create.side_effect = Exception("Position invalid")

    review = ParsedReview(
        comments=[ReviewComment(file="bad.py", line=999, severity="error", comment="Bug")],
        summary="Issues found.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review)

    # Inline failed, so fallback note should be created instead
    assert mr.notes.create.call_count == 2  # 1 fallback + 1 summary
    fallback_body = mr.notes.create.call_args_list[0][0][0]["body"]
    assert "Bug" in fallback_body
    assert "bad.py:999" in fallback_body


async def test_posts_comment_with_suggestion() -> None:
    gl = MagicMock()
    review = ParsedReview(
        comments=[ReviewComment(
            file="calc.py", line=8, severity="error",
            comment="Missing type hints",
            suggestion="def add(a: int, b: int) -> int:",
        )],
        summary="Fixes needed.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review)

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    body = mr.discussions.create.call_args[0][0]["body"]
    assert "Missing type hints" in body
    assert "```suggestion:-0+0" in body
    assert "def add(a: int, b: int) -> int:" in body


async def test_posts_comment_with_multiline_suggestion() -> None:
    gl = MagicMock()
    review = ParsedReview(
        comments=[ReviewComment(
            file="calc.py", line=10, severity="warning",
            comment="Refactor block",
            suggestion="    x = 1\n    y = 2",
            suggestion_start_offset=2,
            suggestion_end_offset=1,
        )],
        summary="Done.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review)

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    body = mr.discussions.create.call_args[0][0]["body"]
    assert "```suggestion:-2+1" in body
    assert "    x = 1\n    y = 2" in body


async def test_posts_comment_without_suggestion_has_no_block() -> None:
    gl = MagicMock()
    review = ParsedReview(
        comments=[ReviewComment(file="a.py", line=1, severity="info", comment="Looks fine")],
        summary="Ok.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review)

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    body = mr.discussions.create.call_args[0][0]["body"]
    assert "suggestion" not in body


async def test_both_inline_and_fallback_fail_continues() -> None:
    gl = MagicMock()
    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    mr.discussions.create.side_effect = Exception("Position invalid")
    # Fallback fails for both comments, but summary succeeds
    mr.notes.create.side_effect = [Exception("Fail1"), Exception("Fail2"), None]

    review = ParsedReview(
        comments=[
            ReviewComment(file="a.py", line=1, severity="error", comment="Bug1"),
            ReviewComment(file="b.py", line=2, severity="error", comment="Bug2"),
        ],
        summary="Summary.",
    )
    # Should not raise — both comments attempted, summary still posted
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review)
    assert mr.notes.create.call_count == 3
