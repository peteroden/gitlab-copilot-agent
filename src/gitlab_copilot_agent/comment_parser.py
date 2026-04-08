"""Parse structured review output from the Copilot agent."""

from __future__ import annotations

import json
import re
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = structlog.get_logger()

ResolutionStatus = Literal["resolved", "not_addressed", "partial"]


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


class Resolution(BaseModel):
    """A resolution determination for a prior feedback thread."""

    model_config = ConfigDict(frozen=True)
    discussion_id: str = Field(description="GitLab discussion ID of the prior feedback")
    status: ResolutionStatus = Field(
        description="Resolution status: resolved, not_addressed, or partial"
    )
    message: str = Field(description="Acknowledgment or explanation message")


class ParsedReview(BaseModel):
    """Structured review output with comments, resolutions, and a summary."""

    comments: list[ReviewComment] = Field(description="List of review comments")
    summary: str = Field(description="Summary paragraph of the review")
    resolutions: list[Resolution] = Field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list, description="Resolution determinations for prior feedback"
    )
    parse_path: str = Field(
        default="unknown",
        description="Internal: which parsing strategy produced this result",
    )


def _is_bare_comment(data: dict[str, object]) -> bool:
    """Return True if *data* looks like a single ReviewComment, not a review wrapper."""
    return "file" in data and "line" in data and "comments" not in data


def _build_parsed_review(
    data: dict[str, object], summary: str, *, parse_path: str = "unknown"
) -> ParsedReview:
    """Build a ParsedReview from a parsed JSON dict and summary text.

    Handles the edge case where the LLM emits a single comment object
    (``{"file": …, "line": …}``) instead of ``{"comments": [...]}``.
    """
    # Normalise bare comment object → wrapped format
    if _is_bare_comment(data):
        data = {"comments": [data]}

    comments: list[ReviewComment] = []
    for item in data.get("comments", []):  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportGeneralTypeIssues]
        if not isinstance(item, dict):
            continue
        try:
            comments.append(ReviewComment.model_validate(item))
        except (KeyError, ValueError, ValidationError):
            continue

    resolutions: list[Resolution] = []
    for item in data.get("resolutions", []):  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportGeneralTypeIssues]
        if not isinstance(item, dict):
            continue
        try:
            resolutions.append(Resolution.model_validate(item))
        except (KeyError, ValueError, ValidationError):
            continue

    return ParsedReview(
        comments=comments, summary=summary, resolutions=resolutions, parse_path=parse_path
    )


def parse_review(raw: str) -> ParsedReview:
    """Extract structured comments, resolutions, and summary from agent output.

    Expects a JSON object with "comments" and "resolutions" arrays
    (optionally in a code fence) followed by a summary.
    Falls back to treating the entire output as a summary if parsing fails.
    """
    log.debug("parse_review_input", raw_length=len(raw), raw_preview=raw[:300])

    # Try code-fenced JSON object first
    json_match = re.search(r"```json\s*\n(\{.*?\})\s*\n```", raw, re.DOTALL)
    if json_match:
        try:
            parsed: object = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            log.debug("parse_review_path", path="code_fence_json_error")
            return ParsedReview(
                comments=[], summary=raw.strip(), parse_path="code_fence_json_error"
            )
        if not isinstance(parsed, dict):
            log.debug("parse_review_path", path="code_fence_not_dict")
            return ParsedReview(comments=[], summary=raw.strip(), parse_path="code_fence_not_dict")
        summary = raw[json_match.end() :].strip()
        summary = re.sub(r"^```\s*", "", summary).strip() or "Review complete."
        result = _build_parsed_review(parsed, summary, parse_path="code_fence")  # pyright: ignore[reportUnknownArgumentType]
        log.debug(
            "parse_review_path",
            path="code_fence",
            comments=len(result.comments),
            bare_wrap=_is_bare_comment(parsed),  # pyright: ignore[reportUnknownArgumentType]
        )
        return result

    # Try bare JSON object using raw_decode (handles braces in trailing text)
    stripped = raw.strip()
    idx = stripped.find("{")
    if idx == -1:
        log.debug("parse_review_path", path="freetext_fallback")
        return ParsedReview(
            comments=[], summary=stripped or "Review complete.", parse_path="freetext_fallback"
        )
    try:
        parsed, end_idx = json.JSONDecoder().raw_decode(stripped, idx)
    except json.JSONDecodeError:
        log.debug("parse_review_path", path="raw_decode_json_error")
        return ParsedReview(comments=[], summary=stripped, parse_path="raw_decode_json_error")
    if not isinstance(parsed, dict):
        log.debug("parse_review_path", path="raw_decode_not_dict")
        return ParsedReview(comments=[], summary=stripped, parse_path="raw_decode_not_dict")
    summary = stripped[end_idx:].strip() or "Review complete."
    result = _build_parsed_review(parsed, summary, parse_path="raw_decode")  # pyright: ignore[reportUnknownArgumentType]
    log.debug(
        "parse_review_path",
        path="raw_decode",
        comments=len(result.comments),
        bare_wrap=_is_bare_comment(parsed),  # pyright: ignore[reportUnknownArgumentType]
    )
    return result
