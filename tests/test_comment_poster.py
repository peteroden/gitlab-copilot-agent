"""Tests for comment posting to GitLab."""

from unittest.mock import MagicMock

from gitlab_copilot_agent.comment_parser import ParsedReview, Resolution, ReviewComment
from gitlab_copilot_agent.comment_poster import (
    _build_activity_section,
    _handle_resolutions,
    post_review,
)
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


# -- Activity section tests --

ACTIVITY_HEADER = "### 📊 Review Activity"


def test_build_activity_section_all_zero() -> None:
    """All counts zero → empty string."""
    result = _build_activity_section(0, 0, [], 0)
    assert result == ""


def test_build_activity_section_comments_only() -> None:
    """Only new comments → single bullet with count."""
    result = _build_activity_section(3, 0, [], 0)
    assert ACTIVITY_HEADER in result
    assert "- **3** new comments" in result
    assert "resolved" not in result
    assert "partial" not in result


def test_build_activity_section_single_comment() -> None:
    """Singular form for 1 comment."""
    result = _build_activity_section(1, 0, [], 0)
    assert "- **1** new comment" in result
    assert "comments" not in result


def test_build_activity_section_inline_plus_fallback() -> None:
    """Inline + fallback counts are summed for total."""
    result = _build_activity_section(2, 1, [], 0)
    assert "- **3** new comments" in result


def test_build_activity_section_resolved_only() -> None:
    """Only resolved threads → single bullet."""
    result = _build_activity_section(0, 0, [], 5)
    assert ACTIVITY_HEADER in result
    assert "- **5** threads resolved" in result
    assert "new comment" not in result


def test_build_activity_section_single_resolved() -> None:
    """Singular form for 1 thread resolved."""
    result = _build_activity_section(0, 0, [], 1)
    assert "- **1** thread resolved" in result
    assert "threads" not in result


def test_build_activity_section_partial_only() -> None:
    """Only partial resolutions → single bullet."""
    resolutions = [
        _make_resolution(status="partial", message="Partially fixed"),
        _make_resolution(discussion_id=DISC_ID_TWO, status="partial", message="Also partial"),
    ]
    result = _build_activity_section(0, 0, resolutions, 0)
    assert ACTIVITY_HEADER in result
    assert "- **2** partial resolutions" in result


def test_build_activity_section_single_partial() -> None:
    """Singular form for 1 partial resolution."""
    resolutions = [_make_resolution(status="partial", message="Partial")]
    result = _build_activity_section(0, 0, resolutions, 0)
    assert "- **1** partial resolution" in result
    assert "resolutions" not in result


def test_build_activity_section_all_nonzero() -> None:
    """All counts nonzero → all bullets present."""
    resolutions = [
        _make_resolution(status="partial", message="Partial"),
        _make_resolution(discussion_id=DISC_ID_TWO, status="resolved", message="Fixed"),
    ]
    result = _build_activity_section(2, 1, resolutions, 3)
    assert ACTIVITY_HEADER in result
    assert "- **3** new comments" in result
    assert "- **3** threads resolved" in result
    assert "- **1** partial resolution" in result


def test_build_activity_section_ignores_non_partial_resolutions() -> None:
    """Only 'partial' status counted; resolved and not_addressed are not partial."""
    resolutions = [
        _make_resolution(status="resolved", message="Fixed"),
        _make_resolution(discussion_id=DISC_ID_TWO, status="not_addressed", message="Nope"),
    ]
    result = _build_activity_section(0, 0, resolutions, 0)
    assert result == ""


# -- Summary composition with activity section --


async def test_summary_contains_activity_section_with_comments() -> None:
    """Summary note includes activity section when comments are posted."""
    gl = MagicMock()
    review = ParsedReview(
        comments=[ReviewComment(file="src/main.py", line=2, severity="error", comment="Bug")],
        summary="Issues found.",
    )
    await post_review(
        gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes(), head_sha=SHA_MARKER_VALUE
    )

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    body = mr.notes.create.call_args[0][0]["body"]
    # Activity section between summary and SHA marker
    assert "Issues found." in body
    assert ACTIVITY_HEADER in body
    assert SHA_MARKER_COMMENT in body
    # Verify order: summary → activity → sha marker
    summary_pos = body.index("Issues found.")
    activity_pos = body.index(ACTIVITY_HEADER)
    marker_pos = body.index(SHA_MARKER_COMMENT)
    assert summary_pos < activity_pos < marker_pos


async def test_summary_omits_activity_section_when_zero() -> None:
    """Summary note has no activity section when no comments and no resolutions."""
    gl = MagicMock()
    review = ParsedReview(comments=[], summary="All good.")
    await post_review(
        gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes(), head_sha=SHA_MARKER_VALUE
    )

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    body = mr.notes.create.call_args[0][0]["body"]
    assert ACTIVITY_HEADER not in body
    assert "All good." in body
    assert SHA_MARKER_COMMENT in body


async def test_summary_sha_marker_extractable_with_activity_section() -> None:
    """SHA marker remains extractable even when activity section is present."""
    from gitlab_copilot_agent.incremental import _SHA_MARKER_RE

    gl = MagicMock()
    review = ParsedReview(
        comments=[ReviewComment(file="src/main.py", line=2, severity="error", comment="Bug")],
        summary="Review.",
    )
    await post_review(
        gl, PROJECT_ID, MR_IID, DIFF_REFS, review, make_mr_changes(), head_sha=SHA_MARKER_VALUE
    )

    mr = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)
    body = mr.notes.create.call_args[0][0]["body"]
    match = _SHA_MARKER_RE.search(body)
    assert match is not None
    assert match.group(1) == SHA_MARKER_VALUE


async def test_summary_activity_section_with_resolutions() -> None:
    """Activity section includes resolution stats when resolutions present."""
    gl = MagicMock()
    mr_mock = gl.projects.get(PROJECT_ID).mergerequests.get(MR_IID)

    resolutions = [_make_resolution(status="resolved")]
    review = ParsedReview(comments=[], summary="Review.", resolutions=resolutions)
    await post_review(
        gl,
        PROJECT_ID,
        MR_IID,
        DIFF_REFS,
        review,
        make_mr_changes(),
        resolution_behavior="auto-resolve",
        allowed_discussion_ids=frozenset({DISC_ID_ONE}),
        head_sha=SHA_MARKER_VALUE,
    )

    body = mr_mock.notes.create.call_args[0][0]["body"]
    assert ACTIVITY_HEADER in body
    assert "thread" in body
    assert "resolved" in body
    assert SHA_MARKER_COMMENT in body
