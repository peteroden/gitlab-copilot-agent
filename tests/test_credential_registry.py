"""Tests for credential registry — alias resolution and env var loading."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.credential_registry import CredentialRegistry
from gitlab_copilot_agent.discussion_models import AgentIdentity

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TOKEN = "glpat-default-token"
PLATFORM_TOKEN = "glpat-platform-token"
OPS_TOKEN = "glpat-ops-token"

GITLAB_URL = "https://gitlab.example.com"
AGENT_USER_ID = 100
AGENT_USERNAME = "agent-bot"
OPS_USER_ID = 200
OPS_USERNAME = "ops-bot"


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_default_only(self) -> None:
        reg = CredentialRegistry(default_token=DEFAULT_TOKEN)
        assert reg.resolve("default") == DEFAULT_TOKEN
        assert reg.aliases() == {"default"}

    def test_named_tokens(self) -> None:
        reg = CredentialRegistry(
            default_token=DEFAULT_TOKEN,
            named_tokens={"platform_team": PLATFORM_TOKEN},
        )
        assert reg.resolve("platform_team") == PLATFORM_TOKEN
        assert reg.resolve("default") == DEFAULT_TOKEN

    def test_alias_case_insensitive(self) -> None:
        reg = CredentialRegistry(
            default_token=DEFAULT_TOKEN,
            named_tokens={"Platform_Team": PLATFORM_TOKEN},
        )
        assert reg.resolve("platform_team") == PLATFORM_TOKEN
        assert reg.resolve("PLATFORM_TEAM") == PLATFORM_TOKEN


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_unknown_ref_raises(self) -> None:
        reg = CredentialRegistry(default_token=DEFAULT_TOKEN)
        with pytest.raises(KeyError, match="Unknown credential_ref 'missing'"):
            reg.resolve("missing")

    def test_error_lists_available(self) -> None:
        reg = CredentialRegistry(
            default_token=DEFAULT_TOKEN,
            named_tokens={"ops": OPS_TOKEN},
        )
        with pytest.raises(KeyError, match="default.*ops"):
            reg.resolve("nonexistent")


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_loads_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", DEFAULT_TOKEN)
        reg = CredentialRegistry.from_env()
        assert reg.resolve("default") == DEFAULT_TOKEN

    def test_loads_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", DEFAULT_TOKEN)
        monkeypatch.setenv("GITLAB_TOKEN__PLATFORM_TEAM", PLATFORM_TOKEN)
        monkeypatch.setenv("GITLAB_TOKEN__OPS", OPS_TOKEN)
        reg = CredentialRegistry.from_env()
        assert reg.resolve("platform_team") == PLATFORM_TOKEN
        assert reg.resolve("ops") == OPS_TOKEN

    def test_missing_default_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with pytest.raises(ValueError, match="GITLAB_TOKEN is required"):
            CredentialRegistry.from_env()

    def test_ignores_empty_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", DEFAULT_TOKEN)
        monkeypatch.setenv("GITLAB_TOKEN__EMPTY", "")
        reg = CredentialRegistry.from_env()
        assert reg.aliases() == {"default"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_identity(user_id: int = AGENT_USER_ID, username: str = AGENT_USERNAME) -> AgentIdentity:
    return AgentIdentity(user_id=user_id, username=username)


# ---------------------------------------------------------------------------
# resolve_identity
# ---------------------------------------------------------------------------

_FETCH_PATH = "gitlab_copilot_agent.credential_registry._fetch_identity"


class TestResolveIdentity:
    async def test_returns_identity_on_first_call(self) -> None:
        reg = CredentialRegistry(default_token=DEFAULT_TOKEN)
        expected = _make_identity()
        with patch(_FETCH_PATH, new_callable=AsyncMock, return_value=expected) as mock_fetch:
            result = await reg.resolve_identity("default", GITLAB_URL)

        assert result == expected
        mock_fetch.assert_awaited_once_with(GITLAB_URL, DEFAULT_TOKEN)

    async def test_returns_cached_on_second_call(self) -> None:
        reg = CredentialRegistry(default_token=DEFAULT_TOKEN)
        expected = _make_identity()
        with patch(_FETCH_PATH, new_callable=AsyncMock, return_value=expected) as mock_fetch:
            first = await reg.resolve_identity("default", GITLAB_URL)
            second = await reg.resolve_identity("default", GITLAB_URL)

        assert first == expected
        assert second is first
        mock_fetch.assert_awaited_once()

    async def test_caches_per_credential_ref(self) -> None:
        reg = CredentialRegistry(
            default_token=DEFAULT_TOKEN,
            named_tokens={"ops": OPS_TOKEN},
        )
        default_id = _make_identity()
        ops_id = _make_identity(user_id=OPS_USER_ID, username=OPS_USERNAME)

        async def _fake_fetch(_url: str, token: str) -> AgentIdentity:
            if token == DEFAULT_TOKEN:
                return default_id
            return ops_id

        with patch(_FETCH_PATH, new_callable=AsyncMock, side_effect=_fake_fetch) as mock_fetch:
            result_default = await reg.resolve_identity("default", GITLAB_URL)
            result_ops = await reg.resolve_identity("ops", GITLAB_URL)

        assert result_default == default_id
        assert result_ops == ops_id
        assert mock_fetch.await_count == 2

    async def test_raises_on_unknown_credential_ref(self) -> None:
        reg = CredentialRegistry(default_token=DEFAULT_TOKEN)
        with pytest.raises(KeyError, match="Unknown credential_ref 'missing'"):
            await reg.resolve_identity("missing", GITLAB_URL)

    async def test_cache_is_case_insensitive(self) -> None:
        reg = CredentialRegistry(default_token=DEFAULT_TOKEN)
        expected = _make_identity()
        with patch(_FETCH_PATH, new_callable=AsyncMock, return_value=expected) as mock_fetch:
            first = await reg.resolve_identity("DEFAULT", GITLAB_URL)
            second = await reg.resolve_identity("default", GITLAB_URL)

        assert first == expected
        assert second is first
        mock_fetch.assert_awaited_once()

    async def test_concurrent_calls_only_fetch_once(self) -> None:
        """Multiple concurrent resolve_identity calls for the same ref
        result in only one API call (lock prevents thundering herd)."""
        reg = CredentialRegistry(default_token=DEFAULT_TOKEN)
        expected = _make_identity()

        call_count = 0

        async def _slow_fetch(gitlab_url: str, token: str) -> AgentIdentity:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return expected

        with patch(_FETCH_PATH, side_effect=_slow_fetch):
            results = await asyncio.gather(
                reg.resolve_identity("default", GITLAB_URL),
                reg.resolve_identity("default", GITLAB_URL),
                reg.resolve_identity("default", GITLAB_URL),
            )

        assert all(r == expected for r in results)
        assert call_count == 1
