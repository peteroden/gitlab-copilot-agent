"""Tests for the health check endpoint."""

import pytest
from httpx import AsyncClient


@pytest.mark.usefixtures("env_vars")
async def test_health_returns_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
