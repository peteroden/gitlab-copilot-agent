"""Credential registry — resolves credential aliases to GitLab tokens.

Reads ``GITLAB_TOKEN`` (the default) and ``GITLAB_TOKEN__<ALIAS>`` env vars
at construction time.  A binding's ``credential_ref`` is resolved to the
matching token via :meth:`resolve`.

Also provides lazy identity caching: each credential can be resolved to the
GitLab user it authenticates as via :meth:`resolve_identity`.

No raw secrets are ever logged.
"""

from __future__ import annotations

import asyncio
import os
import re

import gitlab
import structlog

from gitlab_copilot_agent.discussion_models import AgentIdentity

log = structlog.get_logger()

_ALIAS_PATTERN = re.compile(r"^GITLAB_TOKEN__(.+)$")


class CredentialRegistry:
    """Startup-loaded registry mapping credential aliases to tokens."""

    def __init__(self, *, default_token: str, named_tokens: dict[str, str] | None = None) -> None:
        self._tokens: dict[str, str] = {"default": default_token}
        for alias, token in (named_tokens or {}).items():
            self._tokens[alias.lower()] = token
        self._identities: dict[str, AgentIdentity] = {}
        self._identity_lock = asyncio.Lock()

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

    async def resolve_identity(self, credential_ref: str, gitlab_url: str) -> AgentIdentity:
        """Return the :class:`AgentIdentity` for *credential_ref*, with caching.

        On the first call for a given *credential_ref* this authenticates
        against the GitLab API; subsequent calls return the cached result.

        Uses an asyncio lock to prevent thundering-herd duplicate API calls
        when multiple coroutines resolve the same credential concurrently.
        """
        ref = credential_ref.lower()
        cached = self._identities.get(ref)
        if cached is not None:
            return cached

        async with self._identity_lock:
            # Double-check after acquiring the lock
            cached = self._identities.get(ref)
            if cached is not None:
                return cached

            token = self.resolve(credential_ref)
            identity = await _fetch_identity(gitlab_url, token)
            self._identities[ref] = identity
            log.info(
                "agent_identity_resolved",
                credential_ref=ref,
                user_id=identity.user_id,
                username=identity.username,
            )
            return identity

    def aliases(self) -> set[str]:
        """Return all registered credential aliases."""
        return set(self._tokens)


async def _fetch_identity(gitlab_url: str, token: str) -> AgentIdentity:
    """Authenticate against GitLab and return the caller's identity.

    Uses :func:`asyncio.to_thread` because python-gitlab is synchronous.
    """

    def _sync_auth() -> AgentIdentity:
        gl = gitlab.Gitlab(gitlab_url, private_token=token)
        gl.auth()
        user = gl.user
        if user is None:  # pragma: no cover – defensive
            msg = "gl.auth() succeeded but gl.user is None"
            raise RuntimeError(msg)
        return AgentIdentity(user_id=user.id, username=user.username)

    return await asyncio.to_thread(_sync_auth)
