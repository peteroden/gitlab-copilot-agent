"""Tests for container — AppContext dataclass and get_services dependency."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from starlette.datastructures import State

from gitlab_copilot_agent.container import AppContext, get_services


def _make_context(**overrides: object) -> AppContext:
    """Create an AppContext with mock services for testing."""
    defaults = {
        "settings": MagicMock(),
        "executor": MagicMock(),
        "repo_locks": MagicMock(),
        "dedup_store": MagicMock(),
        "review_tracker": MagicMock(),
        "credential_registry": MagicMock(),
    }
    return AppContext(**(defaults | overrides))


def _make_request(app: FastAPI) -> Request:
    """Create a minimal Request object for testing."""
    scope = {"type": "http", "app": app}
    return Request(scope)


class TestAppContext:
    def test_creates_with_required_fields(self) -> None:
        ctx = _make_context()
        assert ctx.settings is not None
        assert ctx.executor is not None
        assert ctx.allowed_project_ids is None

    def test_frozen(self) -> None:
        ctx = _make_context()
        with pytest.raises(AttributeError):
            ctx.settings = MagicMock()  # type: ignore[misc]

    def test_allowed_project_ids_frozenset(self) -> None:
        ctx = _make_context(allowed_project_ids=frozenset({1, 2, 3}))
        assert ctx.allowed_project_ids == frozenset({1, 2, 3})


class TestGetServices:
    def test_returns_container(self) -> None:
        app = FastAPI()
        ctx = _make_context()
        app.state.container = ctx
        request = _make_request(app)
        assert get_services(request) is ctx

    def test_raises_when_no_container(self) -> None:
        app = FastAPI()
        app.state = State()
        request = _make_request(app)
        with pytest.raises(RuntimeError, match="AppContext not initialized"):
            get_services(request)
