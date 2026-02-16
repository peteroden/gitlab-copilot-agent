"""Basic tests for the Blog Post API."""

from fastapi.testclient import TestClient

from demo_app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_read_post_not_found():
    resp = client.get("/posts/nonexistent")
    assert resp.status_code == 404


def test_list_posts_empty():
    resp = client.get("/posts")
    assert resp.status_code == 200
    assert resp.json() == []
