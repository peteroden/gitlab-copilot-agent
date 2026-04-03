"""User-facing error messages for GitLab comments.

Maps internal error details to actionable, user-friendly messages that
never leak stack traces, tokens, or internal implementation details.
"""

from __future__ import annotations

_AUTH_MSG = (
    "❌ Unable to process your request due to an authentication issue.\n\n"
    "The agent's credentials may be expired or misconfigured. "
    "Please notify the project administrator to check the service configuration."
)

_PERMISSION_MSG = (
    "❌ Unable to process your request due to a permissions issue.\n\n"
    "The agent's GitLab token may lack the required scopes "
    "(api, read_repository, write_repository) "
    "or the token role may be insufficient (Developer or higher required)."
)

_CLONE_MSG = (
    "❌ Unable to clone the repository.\n\n"
    "The agent's GitLab token may not have read_repository access "
    "to this project."
)

_TIMEOUT_MSG = (
    "❌ The request timed out.\n\n"
    "The agent took too long to process your request. "
    "Please try again with a simpler request."
)

_PATCH_MSG = (
    "❌ Unable to apply code changes.\n\n"
    "The generated patch could not be applied to the current branch. "
    "This may happen if the branch was modified while the agent was working. "
    "Please try again."
)

_FALLBACK_MSG = (
    "❌ Unable to process your request.\n\n"
    "An unexpected error occurred. "
    "Please try again or contact the project administrator."
)

# Order matters — first match wins.
_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("authentication failed", "github_token"), _AUTH_MSG),
    (("403", "forbidden"), _PERMISSION_MSG),
    (("clone failed", "unable to access"), _CLONE_MSG),
    (("timeout", "timed out"), _TIMEOUT_MSG),
    (("corrupt patch", "git apply"), _PATCH_MSG),
)


def user_error_message(error: str) -> str:
    """Map an internal error string to a user-friendly GitLab comment.

    The returned message is safe to post publicly — it contains no
    stack traces or internal details.
    """
    lower = error.lower()
    for keywords, message in _PATTERNS:
        if any(kw in lower for kw in keywords):
            return message
    return _FALLBACK_MSG
