"""Tests for comment parsing."""

from gitlab_copilot_agent.comment_parser import (
    ParsedReview,
    Resolution,
    ReviewComment,
    _is_bare_comment,
    parse_review,
)

DISCUSSION_ID_ALPHA = "disc_abc123"
DISCUSSION_ID_BETA = "disc_def456"
RESOLUTION_STATUS_RESOLVED = "resolved"
RESOLUTION_MSG = "Acknowledged — fix verified"


def test_parse_structured_json_in_code_fence() -> None:
    raw = (
        "Here is my review:\n```json\n"
        '{"comments": [{"file": "src/main.py", "line": 10, "severity": "error", '
        '"comment": "Missing null check"}], "resolutions": []}\n```\n'
        "Overall the code needs work."
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].file == "src/main.py"
    assert result.comments[0].line == 10
    assert result.comments[0].severity == "error"
    assert result.comments[0].comment == "Missing null check"
    assert "needs work" in result.summary


def test_parse_bare_json_object() -> None:
    raw = (
        '{"comments": [{"file": "a.py", "line": 1, "severity": "info", '
        '"comment": "ok"}], "resolutions": []}\nLooks good.'
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert "Looks good" in result.summary


def test_parse_empty_object() -> None:
    raw = '```json\n{"comments": [], "resolutions": []}\n```\nAll good, no issues found.'
    result = parse_review(raw)
    assert len(result.comments) == 0
    assert "no issues" in result.summary


def test_parse_freetext_fallback() -> None:
    raw = "The code looks great, no issues to report."
    result = parse_review(raw)
    assert len(result.comments) == 0
    assert "looks great" in result.summary


def test_parse_malformed_json_fallback() -> None:
    raw = "```json\n{broken json}\n```\nSome summary."
    result = parse_review(raw)
    assert len(result.comments) == 0
    assert len(result.summary) > 0


def test_parse_skips_invalid_items() -> None:
    raw = (
        "```json\n"
        '{"comments": [{"file": "a.py", "line": 5, "comment": "good"}, '
        '"not an object", '
        '{"missing_required": true}], "resolutions": []}\n```\nDone.'
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].severity == "info"  # default


def test_parse_comment_with_suggestion() -> None:
    raw = (
        "```json\n"
        '{"comments": [{"file": "calc.py", "line": 8, "severity": "error", '
        '"comment": "Missing type hints", '
        '"suggestion": "def add(a: int, b: int) -> int:"}], "resolutions": []}\n```\nDone.'
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    c = result.comments[0]
    assert c.suggestion == "def add(a: int, b: int) -> int:"
    assert c.suggestion_start_offset == 0
    assert c.suggestion_end_offset == 0


def test_parse_comment_with_multiline_suggestion() -> None:
    raw = (
        "```json\n"
        '{"comments": [{"file": "calc.py", "line": 10, "severity": "warning", '
        '"comment": "Refactor block", '
        '"suggestion": "    x = 1\\n    y = 2", '
        '"suggestion_start_offset": 2, "suggestion_end_offset": 1}], "resolutions": []}'
        "\n```\nDone."
    )
    result = parse_review(raw)
    c = result.comments[0]
    assert c.suggestion == "    x = 1\n    y = 2"
    assert c.suggestion_start_offset == 2
    assert c.suggestion_end_offset == 1


def test_parse_comment_without_suggestion_has_none() -> None:
    raw = (
        "```json\n"
        '{"comments": [{"file": "a.py", "line": 1, "severity": "info", '
        '"comment": "Looks fine"}], "resolutions": []}\n```\nOk.'
    )
    result = parse_review(raw)
    assert result.comments[0].suggestion is None
    assert result.comments[0].suggestion_start_offset == 0


# ---------------------------------------------------------------------------
# Resolution model tests
# ---------------------------------------------------------------------------


def test_resolution_model_valid() -> None:
    r = Resolution(
        discussion_id=DISCUSSION_ID_ALPHA,
        status=RESOLUTION_STATUS_RESOLVED,
        message=RESOLUTION_MSG,
    )
    assert r.discussion_id == DISCUSSION_ID_ALPHA
    assert r.status == RESOLUTION_STATUS_RESOLVED
    assert r.message == RESOLUTION_MSG


def test_parsed_review_empty_resolutions_by_default() -> None:
    review = ParsedReview(
        comments=[
            ReviewComment(file="a.py", line=1, comment="ok"),
        ],
        summary="All good.",
    )
    assert review.resolutions == []


def test_parse_review_object_with_resolutions() -> None:
    """JSON object with comments and resolutions → both extracted."""
    raw = (
        "```json\n"
        '{"comments": [{"file": "src/main.py", "line": 10, "severity": "warning", '
        '"comment": "Consider error handling"}], '
        '"resolutions": [{"discussion_id": "' + DISCUSSION_ID_ALPHA + '", '
        '"status": "' + RESOLUTION_STATUS_RESOLVED + '", '
        '"message": "' + RESOLUTION_MSG + '"}]}\n'
        "```\n"
        "Overall the changes look reasonable."
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].file == "src/main.py"
    assert len(result.resolutions) == 1
    assert result.resolutions[0].discussion_id == DISCUSSION_ID_ALPHA
    assert result.resolutions[0].status == RESOLUTION_STATUS_RESOLVED
    assert result.resolutions[0].message == RESOLUTION_MSG
    assert "reasonable" in result.summary


def test_parse_review_object_empty_resolutions() -> None:
    """JSON object with empty resolutions → empty list."""
    raw = (
        "```json\n"
        '{"comments": [{"file": "a.py", "line": 1, "severity": "info", '
        '"comment": "ok"}], "resolutions": []}\n```\nLooks good.'
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.resolutions == []


def test_parse_review_summary_after_json() -> None:
    """Text after JSON block is extracted as summary."""
    raw = (
        '```json\n{"comments": [], "resolutions": []}\n```\nThe code looks clean. No issues found.'
    )
    result = parse_review(raw)
    assert result.comments == []
    assert result.resolutions == []
    assert "No issues found" in result.summary


def test_parsed_review_with_resolutions() -> None:
    resolutions = [
        Resolution(
            discussion_id=DISCUSSION_ID_ALPHA,
            status=RESOLUTION_STATUS_RESOLVED,
            message=RESOLUTION_MSG,
        ),
        Resolution(
            discussion_id=DISCUSSION_ID_BETA,
            status="partial",
            message="Partially addressed",
        ),
    ]
    review = ParsedReview(
        comments=[],
        summary="Review complete.",
        resolutions=resolutions,
    )
    assert len(review.resolutions) == 2
    assert review.resolutions[0].discussion_id == DISCUSSION_ID_ALPHA
    assert review.resolutions[1].status == "partial"


def test_parse_review_bare_json_with_braces_in_summary() -> None:
    """Bare JSON followed by summary containing braces → comments extracted correctly."""
    raw = (
        '{"comments": [{"file": "src/app.py", "line": 5, "severity": "warning", '
        '"comment": "Missing validation"}], "resolutions": []}'
        "\nHere is a code sample with braces: `if x { return y; }` end."
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].file == "src/app.py"
    assert result.comments[0].line == 5
    assert result.comments[0].comment == "Missing validation"
    assert "braces" in result.summary
    assert "{ return y; }" in result.summary


# ── Bare comment object edge cases ──────────────────────────────────────


def test_is_bare_comment_detects_single_comment_object() -> None:
    """Dict with file+line but no comments key is a bare comment."""
    assert _is_bare_comment({"file": "a.py", "line": 1, "comment": "x"})


def test_is_bare_comment_rejects_wrapped_format() -> None:
    """Dict with 'comments' key is not a bare comment."""
    assert not _is_bare_comment({"comments": [], "resolutions": []})


def test_is_bare_comment_rejects_unrelated_dict() -> None:
    """Dict without file+line is not a bare comment."""
    assert not _is_bare_comment({"summary": "looks good"})


def test_parse_bare_comment_in_code_fence() -> None:
    """LLM returns a single comment object in a code fence instead of wrapped format."""
    raw = (
        "Here is the finding:\n\n```json\n"
        "{\n"
        '  "file": "src/demo_app/main.py",\n'
        '  "line": 13,\n'
        '  "severity": "warning",\n'
        '  "comment": "Missing length constraints on q parameter"\n'
        "}\n"
        "```\n\n"
        "The query parameter needs validation."
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].file == "src/demo_app/main.py"
    assert result.comments[0].line == 13
    assert result.comments[0].severity == "warning"
    assert "length constraints" in result.comments[0].comment
    assert "validation" in result.summary


def test_parse_bare_comment_with_suggestion_in_code_fence() -> None:
    """Bare comment object with suggestion fields is correctly extracted."""
    raw = (
        "```json\n"
        "{\n"
        '  "file": "src/app.py",\n'
        '  "line": 5,\n'
        '  "severity": "error",\n'
        '  "comment": "SQL injection via f-string",\n'
        '  "suggestion": "cursor.execute(\\"SELECT * FROM t WHERE id = ?\\", (uid,))",\n'
        '  "suggestion_start_offset": 0,\n'
        '  "suggestion_end_offset": 0\n'
        "}\n"
        "```\n"
        "Fix the SQL injection."
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].suggestion is not None
    assert "execute" in result.comments[0].suggestion


def test_parse_bare_comment_via_raw_decode() -> None:
    """Bare comment object without code fence is extracted via raw_decode path."""
    raw = (
        '{"file": "src/search.py", "line": 11, "severity": "error", '
        '"comment": "SQL injection"}\n'
        "This is a critical finding."
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].file == "src/search.py"
    assert result.comments[0].line == 11
