# 0011. Typed AppContext Replacing app.state Service Locator

## Status

Accepted

## Context

The FastAPI application stored all runtime services on `app.state` as untyped attributes — `app.state.settings`, `app.state.executor`, `app.state.credential_registry`, etc. (10 attributes total). Consumers accessed them via `request.app.state.settings` (no type safety) or `getattr(request.app.state, "project_registry", None)` (no compile-time checking). This pattern had several problems:

1. **No type safety** — pyright couldn't verify that accessed attributes existed or had correct types
2. **Implicit coupling** — no single place documented which services existed
3. **Test fragility** — tests set/deleted individual `app.state` attributes with no factory or schema
4. **Silent failures** — misspelling an attribute name produced `AttributeError` at runtime

## Options Considered

### Option A: Keep app.state, add type stubs

Add a custom `State` subclass with typed attributes.

- Pros: Minimal refactoring
- Cons: Starlette's `State` uses `__getattr__` internally, defeating most static analysis. Still requires `getattr` for optional attributes.

### Option B: Frozen dataclass on app.state

Create a `@dataclass(frozen=True)` `AppContext` with typed fields for all services. Store it as `app.state.app_context`. Provide `get_app_context(request)` as a FastAPI `Depends()` accessor.

- Pros: Full type safety. Single source of truth for service inventory. Immutability prevents accidental mutation. `get_app_context()` provides a clean injection point.
- Cons: Mutable state (project_registry, pollers) can't live in a frozen dataclass. Hot-reload endpoint needs to swap project_registry.

### Option C: Full DI container (dependency-injector, etc.)

Use a third-party DI framework.

- Pros: Established patterns, scope management
- Cons: New dependency for a simple service set. Over-engineered for ~10 services. Framework lock-in.

## Decision

**Option B** — Frozen `AppContext` dataclass with mutable state kept separately.

Immutable services (`settings`, `executor`, `repo_locks`, `dedup_store`, `dedup`, `credential_registry`, `allowed_project_ids`) live in the frozen `AppContext` at `app.state.app_context`. Mutable state (`project_registry`, `jira_poller`, `gl_poller`) stays on `app.state` directly to support hot-reload via `/config/reload`.

`get_app_context(request: Request) -> AppContext` is the FastAPI dependency for typed access. It raises `RuntimeError` with a clear message if the context isn't initialized.

Tests use `make_app_context(**overrides)` factory and `dataclasses.replace()` for per-test overrides.

## Consequences

- All `getattr(app.state, ...)` calls in gitlab_webhook.py replaced with `get_app_context(request)`
- `project_registry` access remains via `request.app.state.project_registry` (mutable, may be None)
- Test fixtures simplified: `make_app_context()` replaces 7 individual `app.state.X = Y` lines
- `pyright: ignore[reportPrivateUsage]` remains in `/config/reload` for poller internal mutation (hot-reload design constraint)
- Future phases may introduce an `AtomicRef` wrapper to bring mutable state into a typed container
