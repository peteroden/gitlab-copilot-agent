"""URL validation and sanitization for git clone operations."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

# Patterns indicating transient clone errors worth retrying
TRANSIENT_PATTERNS = [
    re.compile(r"The requested URL returned error: 403", re.IGNORECASE),
    re.compile(r"The requested URL returned error: 5\d{2}", re.IGNORECASE),
    re.compile(r"HTTP/\d[\d.]* 5\d{2}", re.IGNORECASE),
    re.compile(r"connection refused", re.IGNORECASE),
    re.compile(r"timed out", re.IGNORECASE),
    re.compile(r"Could not resolve host", re.IGNORECASE),
]

# Patterns indicating permanent errors that should NOT be retried
PERMANENT_PATTERNS = [
    re.compile(r"repository not found", re.IGNORECASE),
    re.compile(r"not valid", re.IGNORECASE),
    re.compile(r"The requested URL returned error: 401", re.IGNORECASE),
    re.compile(r"The requested URL returned error: 404", re.IGNORECASE),
]


def validate_clone_url(url: str) -> None:
    """Validate clone URL is HTTPS and has no embedded credentials.

    Raises:
        ValueError: If URL is invalid, not HTTPS, or contains credentials.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Invalid URL format: {e}") from e

    _allow_http = os.environ.get("ALLOW_HTTP_CLONE", "").lower() in ("true", "1", "yes")
    if parsed.scheme == "http" and _allow_http:
        pass  # E2E testing with mock git server
    elif parsed.scheme != "https":
        raise ValueError(f"Clone URL must use HTTPS scheme, got: {parsed.scheme}")

    if parsed.username or parsed.password:
        raise ValueError("Clone URL must not contain embedded credentials")

    if not parsed.netloc or not parsed.path:
        raise ValueError("Clone URL must have valid host and path")


def validate_clone_url_host(clone_url: str, gitlab_url: str) -> None:
    """Ensure the clone URL belongs to the configured GitLab instance.

    Prevents token exfiltration via forged webhook payloads that redirect
    ``git clone`` (with embedded token) to an attacker-controlled host.

    Raises:
        ValueError: If the clone URL host doesn't match the GitLab instance.
    """
    clone_parsed = urlparse(clone_url)
    gitlab_parsed = urlparse(gitlab_url)
    if clone_parsed.netloc.lower() != gitlab_parsed.netloc.lower():
        raise ValueError(
            f"Clone URL host '{clone_parsed.netloc}' does not match "
            f"configured GitLab host '{gitlab_parsed.netloc}'"
        )


def sanitize_url_for_log(url: str) -> str:
    """Remove credentials from URL for safe logging."""
    try:
        parsed = urlparse(url)
        if not parsed.scheme:
            return "<invalid-url>"
        if parsed.username or parsed.password:
            netloc = parsed.hostname
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return f"{parsed.scheme}://{netloc}{parsed.path}"
        return url
    except Exception:
        return "<invalid-url>"


def is_transient_clone_error(stderr: str) -> bool:
    """Return True if stderr indicates a transient (retryable) clone error."""
    if any(p.search(stderr) for p in PERMANENT_PATTERNS):
        return False
    return any(p.search(stderr) for p in TRANSIENT_PATTERNS)
