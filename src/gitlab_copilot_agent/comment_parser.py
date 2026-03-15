"""Parse structured review output from the Copilot agent."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ReviewComment(BaseModel):
    """A single review comment on a specific file and line."""

    model_config = ConfigDict(frozen=True)
    file: str = Field(description="Path to the reviewed file")
    line: int = Field(description="Line number of the comment")
    severity: str = Field(default="info", description="Severity level: error, warning, or info")
    comment: str = Field(description="Review comment text")
    suggestion: str | None = Field(default=None, description="Suggested replacement code")
    suggestion_start_offset: int = Field(
        default=0, description="Lines above the commented line to replace"
    )
    suggestion_end_offset: int = Field(
        default=0, description="Lines below the commented line to replace"
    )


class ParsedReview(BaseModel):
    """Structured review output with comments and a summary."""

    comments: list[ReviewComment] = Field(description="List of review comments")
    summary: str = Field(description="Summary paragraph of the review")


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
        parsed: object = json.loads(json_match.group(1))
    except json.JSONDecodeError:
        return ParsedReview(comments=[], summary=raw.strip())

    if not isinstance(parsed, list):
        return ParsedReview(comments=[], summary=raw.strip())

    comments: list[ReviewComment] = []
    for item in parsed:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, dict):
            continue
        try:
            comments.append(ReviewComment.model_validate(item))
        except (KeyError, ValueError, ValidationError):
            continue

    summary = raw[json_match.end() :].strip()
    summary = re.sub(r"^```\s*", "", summary).strip() or "Review complete."
    return ParsedReview(comments=comments, summary=summary)
