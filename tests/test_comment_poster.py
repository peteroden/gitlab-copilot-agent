"""Tests for comment posting to GitLab."""

from unittest.mock import MagicMock

from gitlab_copilot_agent.comment_parser import ParsedReview, Resolution, ReviewComment
from gitlab_copilot_agent.comment_poster import _handle_resolutions, post_review
from tests.conftest import DIFF_REFS, MR_IID, PROJECT_ID, make_mr_changes


async def test_posts_inline_and_summary() -> None:
    gl = MagicMock()
    review = ParsedReview(
        comments=[ReviewComment(file="src/main.py", line=2, severity="error", comment="Bug")],
        summary="Needs fixes.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    assert mr.discussions.create.call_count == 1
    assert mr.notes.create.call_count == 1
    assert "Needs fixes" in mr.notes.create.call_args[0][0]["body"]


async def test_posts_summary_only_when_no_comments() -> None:
    gl = MagicMock()
    review = ParsedReview(comments=[], summary="All good.")
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    assert mr.discussions.create.call_count == 0
    assert mr.notes.create.call_count == 1


async def test_inline_failure_falls_back_to_note() -> None:
    gl = MagicMock()
    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    mr.discussions.create.side_effect = Exception("Position invalid")

    review = ParsedReview(
        comments=[ReviewComment(file="src/main.py", line=2, severity="error", comment="Bug")],
        summary="Issues found.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

    # Inline failed, so fallback note should be created instead
    assert mr.notes.create.call_count == 2  # 1 fallback + 1 summary
    fallback_body = mr.notes.create.call_args_list[0][0][0]["body"]
    assert "Bug" in fallback_body
    assert "src/main.py:2" in fallback_body


async def test_posts_comment_with_suggestion() -> None:
    gl = MagicMock()
    review = ParsedReview(
        comments=[
            ReviewComment(
                file="src/main.py",
                line=2,
                severity="error",
                comment="Missing type hints",
                suggestion="def add(a: int, b: int) -> int:",
            )
        ],
        summary="Fixes needed.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    body = mr.discussions.create.call_args[0][0]["body"]
    assert "Missing type hints" in body
    assert "```suggestion:-0+0" in body
    assert "def add(a: int, b: int) -> int:" in body


async def test_posts_comment_with_multiline_suggestion() -> None:
    gl = MagicMock()
    review = ParsedReview(
        comments=[
            ReviewComment(
                file="src/main.py",
                line=2,
                severity="warning",
                comment="Refactor block",
                suggestion="    x = 1\n    y = 2",
                suggestion_start_offset=2,
                suggestion_end_offset=1,
            )
        ],
        summary="Done.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    body = mr.discussions.create.call_args[0][0]["body"]
    assert "```suggestion:-2+1" in body
    assert "    x = 1\n    y = 2" in body


async def test_posts_comment_without_suggestion_has_no_block() -> None:
    gl = MagicMock()
    review = ParsedReview(
        comments=[
            ReviewComment(file="src/main.py", line=1, severity="info", comment="Looks fine")
        ],
        summary="Ok.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

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
            ReviewComment(file="src/main.py", line=1, severity="error", comment="Bug1"),
            ReviewComment(file="src/main.py", line=2, severity="error", comment="Bug2"),
        ],
        summary="Summary.",
    )
    # Should not raise — both comments attempted, summary still posted
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())
    assert mr.notes.create.call_count == 3


async def test_invalid_position_skips_inline_and_posts_fallback() -> None:
    """Invalid positions (not in diff) should skip inline and post fallback note."""
    gl = MagicMock()
    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)

    # Line 999 is not in the sample diff (only lines 1-4 and 11-13 are valid)
    review = ParsedReview(
        comments=[
            ReviewComment(file="src/main.py", line=999, severity="error", comment="Invalid pos")
        ],
        summary="Done.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

    # Should NOT call discussions.create (inline)
    assert mr.discussions.create.call_count == 0
    # Should call notes.create twice: 1 fallback + 1 summary
    assert mr.notes.create.call_count == 2
    fallback_body = mr.notes.create.call_args_list[0][0][0]["body"]
    assert "Invalid pos" in fallback_body
    assert "src/main.py:999" in fallback_body


async def test_valid_position_posts_inline() -> None:
    """Valid positions in diff should post inline comments."""
    gl = MagicMock()

    # Lines 1, 2, 3, 4 are valid in the sample diff (hunk @@ -1,3 +1,4 @@)
    review = ParsedReview(
        comments=[
            ReviewComment(file="src/main.py", line=1, severity="info", comment="Line 1 ok"),
            ReviewComment(file="src/main.py", line=2, severity="info", comment="Line 2 ok"),
        ],
        summary="Done.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    # Both should be inline
    assert mr.discussions.create.call_count == 2
    # Only summary note
    assert mr.notes.create.call_count == 1


async def test_mixed_valid_and_invalid_positions() -> None:
    """Mix of valid and invalid positions should route correctly."""
    gl = MagicMock()

    review = ParsedReview(
        comments=[
            ReviewComment(file="src/main.py", line=2, severity="info", comment="Valid"),
            ReviewComment(file="src/main.py", line=999, severity="error", comment="Invalid"),
            ReviewComment(file="src/main.py", line=12, severity="warning", comment="Also valid"),
        ],
        summary="Mixed.",
    )
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    # 2 valid positions = 2 inline comments
    assert mr.discussions.create.call_count == 2
    # 1 invalid fallback + 1 summary = 2 notes
    assert mr.notes.create.call_count == 2


async def test_file_not_in_changes_is_skipped() -> None:
    """Comments for files not in MR changes should be silently skipped."""
    gl = MagicMock()

    review = ParsedReview(
        comments=[
            ReviewComment(file="other_file.py", line=5, severity="error", comment="Wrong file")
        ],
        summary="Done.",
    )
    # Changes only include src/main.py
    await post_review(gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes())

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    # No inline comment
    assert mr.discussions.create.call_count == 0
    # Only summary note (file not in diff is skipped, not posted as fallback)
    assert mr.notes.create.call_count == 1


# ---------------------------------------------------------------------------
# Resolution handling tests
# ---------------------------------------------------------------------------

DISC_ID_ONE = "disc_res_001"
DISC_ID_TWO = "disc_res_002"
RESOLUTION_MSG_RESOLVED = "Fix verified — error handling added"
RESOLUTION_MSG_PARTIAL = "Null check added but edge case remains"
RESOLUTION_MSG_NOT_ADDRESSED = "Issue still present"


def _make_resolution(
    discussion_id: str = DISC_ID_ONE,
    status: str = "resolved",
    message: str = RESOLUTION_MSG_RESOLVED,
) -> Resolution:
    return Resolution(discussion_id=discussion_id, status=status, message=message)


def test_handle_resolutions_off() -> None:
    """resolution_behavior='off' → no actions taken."""
    mr = MagicMock()
    resolutions = [_make_resolution()]
    result = _handle_resolutions(mr, resolutions, "off")
    assert result == 0
    mr.discussions.get.assert_not_called()


def test_handle_resolutions_auto_resolve_resolved() -> None:
    """auto-resolve + resolved → reply with ✅ + resolve thread."""
    mr = MagicMock()
    disc = mr.discussions.get.return_value
    resolutions = [_make_resolution(status="resolved")]

    result = _handle_resolutions(
        mr,
        resolutions,
        "auto-resolve",
        allowed_discussion_ids=frozenset({DISC_ID_ONE}),
    )

    assert result == 1
    mr.discussions.get.assert_called_once_with(DISC_ID_ONE)
    disc.notes.create.assert_called_once()
    body = disc.notes.create.call_args[0][0]["body"]
    assert "✅" in body
    assert RESOLUTION_MSG_RESOLVED in body
    assert disc.resolved is True
    disc.save.assert_called_once()


def test_handle_resolutions_auto_resolve_partial() -> None:
    """auto-resolve + partial → reply with ⚠️, NO resolve."""
    mr = MagicMock()
    disc = mr.discussions.get.return_value
    resolutions = [_make_resolution(status="partial", message=RESOLUTION_MSG_PARTIAL)]

    result = _handle_resolutions(
        mr,
        resolutions,
        "auto-resolve",
        allowed_discussion_ids=frozenset({DISC_ID_ONE}),
    )

    assert result == 0
    disc.notes.create.assert_called_once()
    body = disc.notes.create.call_args[0][0]["body"]
    assert "⚠️" in body
    assert RESOLUTION_MSG_PARTIAL in body
    disc.save.assert_not_called()


def test_handle_resolutions_suggest_resolved() -> None:
    """suggest + resolved → reply with ✅ only, no resolve."""
    mr = MagicMock()
    disc = mr.discussions.get.return_value
    resolutions = [_make_resolution(status="resolved")]

    result = _handle_resolutions(
        mr,
        resolutions,
        "suggest",
        allowed_discussion_ids=frozenset({DISC_ID_ONE}),
    )

    assert result == 0
    disc.notes.create.assert_called_once()
    body = disc.notes.create.call_args[0][0]["body"]
    assert "✅" in body
    disc.save.assert_not_called()


def test_handle_resolutions_not_addressed_skipped() -> None:
    """not_addressed → no action taken."""
    mr = MagicMock()
    resolutions = [_make_resolution(status="not_addressed", message=RESOLUTION_MSG_NOT_ADDRESSED)]

    result = _handle_resolutions(
        mr,
        resolutions,
        "auto-resolve",
        allowed_discussion_ids=frozenset({DISC_ID_ONE}),
    )

    assert result == 0
    mr.discussions.get.assert_not_called()


def test_handle_resolutions_error_logged(caplog: object) -> None:
    """Exception during resolve → logged, not raised."""
    mr = MagicMock()
    mr.discussions.get.side_effect = Exception("API error")
    resolutions = [_make_resolution()]

    result = _handle_resolutions(
        mr,
        resolutions,
        "auto-resolve",
        allowed_discussion_ids=frozenset({DISC_ID_ONE}),
    )

    assert result == 0


async def test_post_review_with_resolution_behavior() -> None:
    """resolution_behavior flows through post_review to _handle_resolutions."""
    gl = MagicMock()
    mr_mock = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    disc_mock = mr_mock.discussions.get.return_value

    resolutions = [_make_resolution()]
    review = ParsedReview(
        comments=[],
        summary="Review complete.",
        resolutions=resolutions,
    )
    await post_review(
        gl,
        PROJECT_ID,
        MR_IID,
        DIFF_REFS,
        review,
        make_mr_changes(),
        resolution_behavior="auto-resolve",
        allowed_discussion_ids=frozenset({DISC_ID_ONE}),
    )

    mr_mock.discussions.get.assert_called_once_with(DISC_ID_ONE)
    disc_mock.notes.create.assert_called_once()
    assert disc_mock.resolved is True
    disc_mock.save.assert_called_once()
    # Summary note still posted
    assert mr_mock.notes.create.call_count == 1


def test_handle_resolutions_rejects_unknown_discussion_id() -> None:
    """Resolution with discussion_id not in allowlist is skipped with warning log."""
    mr = MagicMock()
    resolutions = [_make_resolution(discussion_id="disc_unknown")]
    allowed = frozenset({DISC_ID_ONE, DISC_ID_TWO})

    result = _handle_resolutions(mr, resolutions, "auto-resolve", allowed_discussion_ids=allowed)

    assert result == 0
    mr.discussions.get.assert_not_called()


def test_handle_resolutions_empty_allowlist_blocks_all() -> None:
    """When allowed_discussion_ids is empty, all resolutions are skipped (fail closed)."""
    mr = MagicMock()
    resolutions = [_make_resolution(status="resolved")]

    result = _handle_resolutions(
        mr, resolutions, "auto-resolve", allowed_discussion_ids=frozenset()
    )

    assert result == 0
    mr.discussions.get.assert_not_called()


# -- SHA marker embedding tests --

SHA_MARKER_VALUE = "abc123def"
SHA_MARKER_COMMENT = f"<!-- mr-review-agent: last_reviewed_sha={SHA_MARKER_VALUE} -->"


async def test_summary_note_contains_sha_marker() -> None:
    """post_review with head_sha embeds the SHA marker in the summary note."""
    gl = MagicMock()
    review = ParsedReview(comments=[], summary="Review complete.")
    await post_review(
        gl,
        PROJECT_ID,
        MR_IID,
        DIFF_REFS,
        review,
        make_mr_changes(),
        head_sha=SHA_MARKER_VALUE,
    )

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    assert mr.notes.create.call_count == 1
    body = mr.notes.create.call_args[0][0]["body"]
    assert SHA_MARKER_COMMENT in body


async def test_summary_note_no_marker_when_empty_sha() -> None:
    """post_review with empty head_sha does not embed a SHA marker."""
    gl = MagicMock()
    review = ParsedReview(comments=[], summary="Review complete.")
    await post_review(
        gl,
        PROJECT_ID,
        MR_IID,
        DIFF_REFS,
        review,
        make_mr_changes(),
        head_sha="",
    )

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    body = mr.notes.create.call_args[0][0]["body"]
    assert "<!-- mr-review-agent:" not in body
