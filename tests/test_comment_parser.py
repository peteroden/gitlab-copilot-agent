"""Tests for comment parsing."""

from gitlab_copilot_agent.comment_parser import parse_review


def test_parse_structured_json_in_code_fence() -> None:
    raw = (
        'Here is my review:\n```json\n'
        '[{"file": "src/main.py", "line": 10, "severity": "error", '
        '"comment": "Missing null check"}]\n```\n'
        'Overall the code needs work.'
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].file == "src/main.py"
    assert result.comments[0].line == 10
    assert result.comments[0].severity == "error"
    assert result.comments[0].comment == "Missing null check"
    assert "needs work" in result.summary


def test_parse_bare_json_array() -> None:
    raw = '[{"file": "a.py", "line": 1, "severity": "info", "comment": "ok"}]\nLooks good.'
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert "Looks good" in result.summary


def test_parse_empty_array() -> None:
    raw = '```json\n[]\n```\nAll good, no issues found.'
    result = parse_review(raw)
    assert len(result.comments) == 0
    assert "no issues" in result.summary


def test_parse_freetext_fallback() -> None:
    raw = "The code looks great, no issues to report."
    result = parse_review(raw)
    assert len(result.comments) == 0
    assert "looks great" in result.summary


def test_parse_malformed_json_fallback() -> None:
    raw = '```json\n[{broken json}]\n```\nSome summary.'
    result = parse_review(raw)
    assert len(result.comments) == 0
    assert len(result.summary) > 0


def test_parse_skips_invalid_items() -> None:
    raw = (
        '```json\n'
        '[{"file": "a.py", "line": 5, "comment": "good"}, '
        '"not an object", '
        '{"missing_required": true}]\n```\nDone.'
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].severity == "info"  # default


def test_parse_comment_with_suggestion() -> None:
    raw = (
        '```json\n'
        '[{"file": "calc.py", "line": 8, "severity": "error", '
        '"comment": "Missing type hints", '
        '"suggestion": "def add(a: int, b: int) -> int:"}]\n```\nDone.'
    )
    result = parse_review(raw)
    assert len(result.comments) == 1
    c = result.comments[0]
    assert c.suggestion == "def add(a: int, b: int) -> int:"
    assert c.suggestion_start_offset == 0
    assert c.suggestion_end_offset == 0


def test_parse_comment_with_multiline_suggestion() -> None:
    raw = (
        '```json\n'
        '[{"file": "calc.py", "line": 10, "severity": "warning", '
        '"comment": "Refactor block", '
        '"suggestion": "    x = 1\\n    y = 2", '
        '"suggestion_start_offset": 2, "suggestion_end_offset": 1}]\n```\nDone.'
    )
    result = parse_review(raw)
    c = result.comments[0]
    assert c.suggestion == "    x = 1\n    y = 2"
    assert c.suggestion_start_offset == 2
    assert c.suggestion_end_offset == 1


def test_parse_comment_without_suggestion_has_none() -> None:
    raw = (
        '```json\n'
        '[{"file": "a.py", "line": 1, "severity": "info", "comment": "Looks fine"}]\n```\nOk.'
    )
    result = parse_review(raw)
    assert result.comments[0].suggestion is None
    assert result.comments[0].suggestion_start_offset == 0
