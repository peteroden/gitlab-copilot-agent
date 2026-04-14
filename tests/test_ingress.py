"""Tests for ingress security: path restriction, IP allowlist, rate limiting,
admin auth, body size limit, and JSON error handling."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import given
from hypothesis import strategies as st

from gitlab_copilot_agent.main import (
    MAX_BODY_SIZE,
    _get_client_ip,
    _reload_timestamps,
    app,
)
from tests.conftest import (
    HEADERS,
    MR_PAYLOAD,
    make_app_context,
    make_settings,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# -- Constants --

ADMIN_TOKEN = "admin-secret-token"
RELOAD_BODY = {"mappings": {}}


# -- Fixtures --


@pytest.fixture
async def ingress_client(env_vars: None) -> AsyncIterator[AsyncClient]:
    """Client with standard app context for ingress tests."""
    ctx = make_app_context()
    app.state.app_context = ctx
    app.state.project_registry = None
    app.state.jira_poller = None
    app.state.gl_poller = None
    app.state.webhook_ip_allowlist = []
    app.state.trusted_proxies = []
    _reload_timestamps.clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin_client(env_vars: None) -> AsyncIterator[AsyncClient]:
    """Client with admin_token configured."""
    ctx = make_app_context(settings=make_settings(admin_token=ADMIN_TOKEN))
    app.state.app_context = ctx
    app.state.project_registry = None
    app.state.jira_poller = None
    app.state.gl_poller = None
    app.state.webhook_ip_allowlist = []
    app.state.trusted_proxies = []
    _reload_timestamps.clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# -- Path restriction --


class TestPathRestriction:
    @pytest.mark.parametrize("path", ["/webhook", "/health", "/config/reload"])
    async def test_allowed_paths_not_blocked(self, ingress_client: AsyncClient, path: str) -> None:
        resp = await ingress_client.get(path)
        assert resp.status_code != 404

    @pytest.mark.parametrize(
        "path", ["/docs", "/openapi.json", "/redoc", "/admin", "/anything-else"]
    )
    async def test_non_allowed_paths_return_404(
        self, ingress_client: AsyncClient, path: str
    ) -> None:
        resp = await ingress_client.get(path)
        assert resp.status_code == 404


# -- IP allowlist --


class TestIPAllowlist:
    async def test_empty_allowlist_allows_all(self, ingress_client: AsyncClient) -> None:
        app.state.webhook_ip_allowlist = []
        resp = await ingress_client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.status_code != 403

    async def test_allowed_ip_passes(self, ingress_client: AsyncClient) -> None:
        app.state.webhook_ip_allowlist = [ipaddress.ip_network("127.0.0.0/8")]
        resp = await ingress_client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.status_code != 403

    async def test_denied_ip_returns_403(self, ingress_client: AsyncClient) -> None:
        app.state.webhook_ip_allowlist = [ipaddress.ip_network("10.0.0.0/8")]
        resp = await ingress_client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.status_code == 403

    async def test_allowlist_only_applies_to_webhook(self, ingress_client: AsyncClient) -> None:
        app.state.webhook_ip_allowlist = [ipaddress.ip_network("10.0.0.0/8")]
        resp = await ingress_client.get("/health")
        assert resp.status_code == 200


# -- _get_client_ip --


class TestGetClientIP:
    def _req(self, host: str = "127.0.0.1", xff: str | None = None) -> MagicMock:
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = host
        request.headers = {"x-forwarded-for": xff} if xff else {}
        return request

    _TRUSTED_192: ClassVar[list] = [ipaddress.ip_network("192.168.0.0/16")]

    @pytest.mark.parametrize(
        ("host", "xff", "trusted", "expected"),
        [
            ("10.1.2.3", None, [], "10.1.2.3"),
            ("10.1.2.3", "1.2.3.4", "trusted_192", "10.1.2.3"),
            ("192.168.1.1", "1.2.3.4, 192.168.1.2", "trusted_192", "1.2.3.4"),
            ("192.168.1.1", "8.8.8.8, 10.0.0.1, 192.168.1.2", "trusted_192", "10.0.0.1"),
            ("192.168.1.1", "192.168.2.1, 192.168.3.1", "trusted_192", "192.168.1.1"),
            ("192.168.1.1", None, "trusted_192", "192.168.1.1"),
            ("192.168.1.1", "not-an-ip, 1.2.3.4", "trusted_192", "1.2.3.4"),
        ],
        ids=[
            "no_proxies",
            "untrusted_direct",
            "rightmost",
            "multi_hop",
            "all_trusted",
            "no_xff",
            "malformed",
        ],
    )
    def test_ip_extraction(
        self, host: str, xff: str | None, trusted: str | list, expected: str
    ) -> None:
        trusted_nets = self._TRUSTED_192 if trusted == "trusted_192" else trusted
        result = _get_client_ip(self._req(host, xff), trusted_nets)
        assert result == expected

    def test_ipv6_client(self) -> None:
        result = _get_client_ip(self._req("::1"), [ipaddress.ip_network("::1/128")])
        assert result == "::1"

    def test_no_client_returns_unknown(self) -> None:
        request = MagicMock()
        request.client = None
        assert _get_client_ip(request, []) == "unknown"


# -- Rate limiting --


class TestRateLimiting:
    async def test_first_request_succeeds(self, ingress_client: AsyncClient) -> None:
        resp = await ingress_client.post("/config/reload", json=RELOAD_BODY, headers=HEADERS)
        assert resp.status_code != 429

    async def test_rapid_second_request_returns_429(self, ingress_client: AsyncClient) -> None:
        await ingress_client.post("/config/reload", json=RELOAD_BODY, headers=HEADERS)
        resp = await ingress_client.post("/config/reload", json=RELOAD_BODY, headers=HEADERS)
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


# -- Admin auth --


class TestAdminAuth:
    async def test_correct_token_accepted(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.post(
            "/config/reload", json=RELOAD_BODY, headers={"X-Admin-Token": ADMIN_TOKEN}
        )
        assert resp.status_code != 401

    @pytest.mark.parametrize(
        ("headers", "desc"),
        [
            ({"X-Admin-Token": "wrong"}, "wrong token"),
            ({}, "missing token"),
        ],
        ids=["wrong_token", "missing_token"],
    )
    async def test_bad_token_rejected(
        self, admin_client: AsyncClient, headers: dict, desc: str
    ) -> None:
        resp = await admin_client.post("/config/reload", json=RELOAD_BODY, headers=headers)
        assert resp.status_code == 401

    async def test_fallback_to_webhook_secret(self, ingress_client: AsyncClient) -> None:
        resp = await ingress_client.post("/config/reload", json=RELOAD_BODY, headers=HEADERS)
        assert resp.status_code != 401


# -- Body size + JSON error handling --


class TestBodyAndJSON:
    async def test_normal_body_accepted(self, ingress_client: AsyncClient) -> None:
        resp = await ingress_client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.status_code != 413

    async def test_oversized_body_rejected(self, ingress_client: AsyncClient) -> None:
        big_body = b"x" * (MAX_BODY_SIZE + 1)
        resp = await ingress_client.post(
            "/webhook", content=big_body, headers={**HEADERS, "Content-Type": "application/json"}
        )
        assert resp.status_code == 413

    @pytest.mark.parametrize(
        "content",
        [b"not valid json {{{", b""],
        ids=["malformed", "empty"],
    )
    async def test_bad_json_returns_400(self, ingress_client: AsyncClient, content: bytes) -> None:
        resp = await ingress_client.post(
            "/webhook", content=content, headers={**HEADERS, "Content-Type": "application/json"}
        )
        assert resp.status_code == 400


# -- Hypothesis --


class TestGetClientIPFuzz:
    @given(xff=st.text(), direct_ip=st.text())
    def test_never_raises(self, xff: str, direct_ip: str) -> None:
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = direct_ip
        request.headers = {"x-forwarded-for": xff}
        result = _get_client_ip(request, [])
        assert isinstance(result, str)
