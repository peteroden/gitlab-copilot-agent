"""Tests for credential registry — alias resolution and env var loading."""

from __future__ import annotations

import pytest

from gitlab_copilot_agent.credential_registry import CredentialRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TOKEN = "glpat-default-token"
PLATFORM_TOKEN = "glpat-platform-token"
OPS_TOKEN = "glpat-ops-token"


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
