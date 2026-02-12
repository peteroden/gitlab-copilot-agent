"""Tests for the webhook endpoint."""

import pytest
from httpx import AsyncClient

from tests.conftest import HEADERS, MR_PAYLOAD, make_mr_payload


@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_webhook_rejects_bad_token(client: AsyncClient, token: str | None) -> None:
    headers = {"X-Gitlab-Token": token} if token else {}
    resp = await client.post("/webhook", json=MR_PAYLOAD, headers=headers)
    assert resp.status_code == 401


async def test_webhook_ignores_non_mr_event(client: AsyncClient) -> None:
    resp = await client.post("/webhook", json={"object_kind": "push"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


async def test_webhook_ignores_unhandled_action(client: AsyncClient) -> None:
    payload = make_mr_payload(action="merge")
    resp = await client.post("/webhook", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.parametrize("action", ["open", "update"])
async def test_webhook_queues_handled_actions(client: AsyncClient, action: str) -> None:
    payload = make_mr_payload(action=action)
    resp = await client.post("/webhook", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}
