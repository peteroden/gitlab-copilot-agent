"""Sanitize untrusted content before embedding in LLM prompts.

Provides two public functions:

- ``truncate_untrusted`` — enforce per-field length limits (inline mode).
- ``strip_dangerous_chars`` — remove control characters and bidi overrides
  while preserving legitimate Unicode (ZWJ, ZWNJ for Arabic/Indic/emoji).
"""

from __future__ import annotations

import re

__all__ = ["strip_dangerous_chars", "truncate_untrusted"]

# Per-field character limits for inline prompt mode.
_FIELD_LIMITS: dict[str, int] = {
    "mr_title": 500,
    "mr_description": 5000,
    "note_body": 5000,
    "jira_description": 5000,
    "commit_message": 4000,
}

_DEFAULT_LIMIT = 5000

# Dangerous control characters to strip:
#   - NUL (U+0000)
#   - C0 controls U+0001-U+001F **except** \t (U+0009), \n (U+000A), \r (U+000D)
#   - Bidi overrides U+202A-U+202E
#   - Bidi isolates  U+2066-U+2069
#
# Explicitly NOT stripped:
#   - ZWJ  (U+200D) — used in emoji sequences and Indic scripts
#   - ZWNJ (U+200C) — used in Arabic/Indic scripts
_DANGEROUS_CHARS = re.compile(
    r"[\x00\x01-\x08\x0b\x0c\x0e-\x1f"  # C0 controls except \t \n \r
    r"\u202a-\u202e"  # bidi overrides
    r"\u2066-\u2069]",  # bidi isolates
)


def truncate_untrusted(value: str, field_name: str) -> str:
    """Truncate *value* to the per-field limit for *field_name*.

    When truncation occurs, a notice is appended so the LLM knows the
    content was shortened.  Values within the limit are returned unchanged.
    """
    limit = _FIELD_LIMITS.get(field_name, _DEFAULT_LIMIT)
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n[TRUNCATED — original length: {len(value)} chars]"


def strip_dangerous_chars(value: str) -> str:
    """Remove dangerous control characters and bidi overrides from *value*.

    Preserves newlines, tabs, carriage returns, ZWJ (U+200D), and ZWNJ
    (U+200C) which are legitimate in Arabic, Indic, and emoji text.
    """
    return _DANGEROUS_CHARS.sub("", value)
