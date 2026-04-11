"""Shared validators for config models."""

from __future__ import annotations


def parse_comma_list(v: object) -> object:
    """Parse a comma-separated string into a list, with JSON passthrough."""
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return []
        if v.startswith("["):
            return v  # let pydantic handle JSON
        return [item.strip() for item in v.split(",") if item.strip()]
    return v
