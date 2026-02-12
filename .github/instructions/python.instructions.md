---
applyTo: "**/*.py"
---

# Python Instructions

## Async Patterns

- Use `asyncio.TaskGroup` for structured concurrency. No fire-and-forget tasks.
- `async with` for resource lifecycle: temp dirs, HTTP clients, Copilot sessions.
- Always await or track tasks. Unhandled task exceptions are bugs.
- Use `asyncio.shield()` only when cancellation must be deferred (e.g., cleanup).

## Error Handling

- Exception hierarchy: `ServiceError` base → `GitLabError`, `CopilotError`, `WebhookValidationError`.
- Structured logging with `structlog`. Bind context early: `log = log.bind(project_id=pid, mr_iid=iid)`.
- Retry transient failures (HTTP 429, 5xx) with exponential backoff via `tenacity` or manual loop.
- Fail fast on config/auth errors at startup — don't silently default.

## Pydantic

- All data boundaries use Pydantic models: webhook payloads, API responses, config, internal DTOs.
- `model_validate(data)` over manual dict unpacking.
- `model_config = ConfigDict(strict=True)` for external input models.
- `Field(description="...")` on every field. The model is the documentation.

## Dependency Injection

- FastAPI `Depends()` for all service dependencies.
- `Protocol`-based interfaces for external services (`GitLabClient`, `CopilotClient`).
- Constructor injection — no module-level singletons.
- Create services in app lifespan, inject via `Depends()`.

## Types

- No `Any` without a `# type: ignore[<reason>]` comment.
- `Protocol` over `ABC` for interfaces.
- `TypeAlias` for complex union/generic types.
- All functions fully annotated including return types.
- Target: `mypy --strict` clean.

## Project Layout

```
src/gitlab_copilot_agent/
├── __init__.py          # __all__ exports
├── main.py              # FastAPI app, lifespan
├── config.py            # pydantic-settings
├── models.py            # Shared Pydantic models
├── webhook.py           # POST /webhook
├── gitlab_client.py     # Clone, diff, comments
├── review_engine.py     # Copilot session + tools
├── comment_parser.py    # Parse agent output
├── comment_poster.py    # Post to GitLab
└── orchestrator.py      # Wire webhook → review → post
```

- `src/` layout. Absolute imports only.
- One module per responsibility. Explicit `__init__.py` with `__all__`.

## Testing

- `pytest` + `pytest-asyncio` with `asyncio_mode = "auto"`.
- `httpx.AsyncClient` with `ASGITransport` for endpoint tests.
- Test files: `tests/test_<module>.py`, mirroring source structure.
- Test behavior, not implementation. Mock at the boundary.

### Coverage

- Enforce `--cov-fail-under=90` in `pyproject.toml` `[tool.pytest.ini_options]`.
- Always run with `--cov-report=term-missing` to surface gaps.
- New code must have ≥90% coverage. PRs that drop coverage below threshold are blocked.

### No magic strings

- All repeated test data (URLs, tokens, secrets, payloads) must be module-level constants or `conftest.py` fixtures.
- Never inline string literals that appear in more than one test. Extract to a named constant.
- Name constants descriptively: `GITLAB_URL`, `WEBHOOK_SECRET`, `MR_PAYLOAD` — not `URL` or `DATA`.

### Shared fixtures in conftest.py

- `tests/conftest.py` owns shared constants, fixtures, and factory functions.
- Factory pattern for test data: `make_settings(**overrides)` returns a valid object with sensible defaults; tests override only what they care about.
- Shared fixtures: `env_vars` (sets env), `client` (ASGITransport), etc.
- Test files import constants from `conftest.py` — never redefine them.

### Test layers

| Layer | Scope | Mocking | Speed |
|-------|-------|---------|-------|
| Unit | Single function/class | Mock all external deps at boundary | <1s per test |
| Integration | Multiple modules wired together | Mock only external services (GitLab API, Copilot SDK) | <5s per test |
| E2E | Full service | Real services (or containers) | <60s per test |

### conftest.py pattern

```python
# tests/conftest.py
import pytest
from collections.abc import AsyncIterator
from httpx import ASGITransport, AsyncClient
from my_app.config import Settings
from my_app.main import app

# -- Constants (single source of truth) --
GITLAB_URL = "https://gitlab.example.com"
GITLAB_TOKEN = "test-token"
WEBHOOK_SECRET = "test-secret"
HEADERS = {"X-Gitlab-Token": WEBHOOK_SECRET}

MR_PAYLOAD = {
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "testuser"},
    # ...complete payload...
}

# -- Factories --
def make_settings(**overrides: object) -> Settings:
    defaults = {
        "gitlab_url": GITLAB_URL,
        "gitlab_token": GITLAB_TOKEN,
        "gitlab_webhook_secret": WEBHOOK_SECRET,
    }
    return Settings(**(defaults | overrides))  # type: ignore[arg-type]

# -- Fixtures --
@pytest.fixture
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", WEBHOOK_SECRET)

@pytest.fixture
async def client(env_vars: None) -> AsyncIterator[AsyncClient]:
    app.state.settings = make_settings()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
```
