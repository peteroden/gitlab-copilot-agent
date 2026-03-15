"""Credential registry — resolves credential aliases to GitLab tokens.

Reads ``GITLAB_TOKEN`` (the default) and ``GITLAB_TOKEN__<ALIAS>`` env vars
at construction time.  A binding's ``credential_ref`` is resolved to the
matching token via :meth:`resolve`.

No raw secrets are ever logged.
"""

from __future__ import annotations

import os
import re

import structlog

log = structlog.get_logger()

_ALIAS_PATTERN = re.compile(r"^GITLAB_TOKEN__(.+)$")


class CredentialRegistry:
    """Startup-loaded registry mapping credential aliases to tokens."""

    def __init__(self, *, default_token: str, named_tokens: dict[str, str] | None = None) -> None:
        self._tokens: dict[str, str] = {"default": default_token}
        for alias, token in (named_tokens or {}).items():
            self._tokens[alias.lower()] = token

    @classmethod
    def from_env(cls) -> CredentialRegistry:
        """Build a registry from the current environment.

        Reads ``GITLAB_TOKEN`` as the default credential, plus any
        ``GITLAB_TOKEN__<ALIAS>`` env vars as named credentials.
        """
        default_token = os.environ.get("GITLAB_TOKEN", "")
        if not default_token:
            msg = "GITLAB_TOKEN is required"
            raise ValueError(msg)

        named: dict[str, str] = {}
        for key, value in os.environ.items():
            m = _ALIAS_PATTERN.match(key)
            if m and value:
                named[m.group(1).lower()] = value

        registry = cls(default_token=default_token, named_tokens=named)
        log.info(
            "credential_registry_loaded",
            aliases=sorted(registry.aliases()),
        )
        return registry

    def resolve(self, credential_ref: str) -> str:
        """Return the token for *credential_ref*, or raise ``KeyError``."""
        ref = credential_ref.lower()
        try:
            return self._tokens[ref]
        except KeyError:
            msg = (
                f"Unknown credential_ref '{credential_ref}'. "
                f"Available: {', '.join(sorted(self._tokens))}"
            )
            raise KeyError(msg) from None

    def aliases(self) -> set[str]:
        """Return all registered credential aliases."""
        return set(self._tokens)
