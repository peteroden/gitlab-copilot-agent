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
- Inject mocks via DI — no `unittest.mock.patch` on internals.
- `httpx.AsyncClient` with `ASGITransport` for endpoint tests.
- Test files: `tests/test_<module>.py`, mirroring source structure.
- Test behavior, not implementation. Mock at the boundary.
