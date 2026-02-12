"""Parse structured review output from the Copilot agent."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewComment:
    file: str
    line: int
    severity: str
    comment: str
    suggestion: str | None = None
    suggestion_start_offset: int = 0
    suggestion_end_offset: int = 0

@dataclass
class ParsedReview:
    comments: list[ReviewComment]
    summary: str


def parse_review(raw: str) -> ParsedReview:
    """Extract structured comments and summary from agent output.

    Expects a JSON array (optionally in a code fence) followed by a summary.
    Falls back to treating the entire output as a summary if parsing fails.
    """
    json_match = re.search(r"```json\s*\n(\[.*?\])\s*\n```", raw, re.DOTALL)
    if not json_match:
        json_match = re.search(r"(\[.*?\])", raw, re.DOTALL)
    if not json_match:
        return ParsedReview(comments=[], summary=raw.strip())

    try:
        items = json.loads(json_match.group(1))
    except json.JSONDecodeError:
        return ParsedReview(comments=[], summary=raw.strip())

    comments = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            suggestion = item.get("suggestion")
            comments.append(ReviewComment(
                file=str(item["file"]),
                line=int(item["line"]),
                severity=str(item.get("severity", "info")),
                comment=str(item["comment"]),
                suggestion=str(suggestion) if suggestion else None,
                suggestion_start_offset=int(item.get("suggestion_start_offset", 0)),
                suggestion_end_offset=int(item.get("suggestion_end_offset", 0)),
            ))
        except (KeyError, ValueError):
            continue

    summary = raw[json_match.end():].strip()
    summary = re.sub(r"^```\s*", "", summary).strip() or "Review complete."
    return ParsedReview(comments=comments, summary=summary)
