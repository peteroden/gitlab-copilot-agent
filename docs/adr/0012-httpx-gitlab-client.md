# 0012. Drop python-gitlab, Rewrite GitLabClient with httpx

## Status

Accepted

## Context

The service used `python-gitlab` (a synchronous SDK) for all GitLab API access. Since the application is fully async (FastAPI + asyncio), every `python-gitlab` call was wrapped in `asyncio.to_thread()` to avoid blocking the event loop. This created several problems:

1. **Sync-in-async wrapping** — every API call required `await asyncio.to_thread(gl.projects.get(...))`, adding boilerplate and hiding the actual HTTP call
2. **Connection management** — `python-gitlab` manages its own `requests.Session`, making it impossible to share connection pools or configure timeouts consistently
3. **Retry complexity** — retry logic had to wrap the `to_thread` call rather than the HTTP request, preventing per-request retry configuration
4. **Type safety** — `python-gitlab` returns dynamic `RESTObject` instances with no static type information; pyright couldn't verify field access

The research analysis identified that pollers were synthesizing `MergeRequestWebhookPayload` objects to reuse webhook-oriented code paths, partly because the GitLab client API was tightly coupled to `python-gitlab`'s object model.

## Options Considered

### Option A: Keep python-gitlab, improve wrapping

Add a typed async wrapper around `python-gitlab` calls.

- Pros: No API rewrite, smaller diff
- Cons: Still sync underneath, still untyped responses, still can't control connection lifecycle

### Option B: Replace with httpx async client

Write a native async `GitLabClient` using `httpx.AsyncClient` with typed Pydantic response models.

- Pros: Native async, typed responses, shared connection pool, configurable retries/timeouts, full control over pagination
- Cons: Must rewrite all API interactions (~15 endpoints)

### Option C: Use gidgetlab (async GitLab library)

- Pros: Async-native, maintained
- Cons: New dependency with smaller community than httpx; still returns untyped dicts

## Decision

**Option B** — Native async `GitLabClient` with httpx.

The client uses `httpx.AsyncClient` internally with `PRIVATE-TOKEN` header authentication. All responses are parsed into Pydantic models (`MRDetails`, `MRChange`, `MRCommit`, `Discussion`, `DiscussionNote`, etc.) providing full type safety. The client owns its connection lifecycle via async context manager protocol.

Key design choices:
- **Protocol-based interface**: `GitLabClientProtocol` allows testing with mock implementations
- **Pagination built-in**: `list_project_mrs`, `list_mr_discussions` handle `per_page`/`page` parameters
- **No `python-gitlab` dependency**: removed from `pyproject.toml`

## Consequences

- All `asyncio.to_thread()` wrappers for GitLab API calls eliminated
- Response types are statically verified by pyright
- `CredentialRegistry` creates `GitLabClient` instances per credential ref with shared configuration
- Tests mock `GitLabClient` directly (or use `GitLabClientProtocol`) instead of patching `gitlab.Gitlab`
- ~15 API endpoints rewritten as typed async methods on `GitLabClient`
