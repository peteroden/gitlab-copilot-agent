# 0014. Unified DeduplicationService with Pluggable Backend

## Status

Accepted

## Context

Deduplication was spread across three independent implementations:

1. **`ReviewedMRTracker`** — tracked `(project_id, mr_iid, head_sha)` tuples for MR reviews, with an Azure Table backend and local set cache
2. **`ProcessedIssueTracker`** — tracked Jira issue keys, with its own Azure Table backend
3. **`DeduplicationStore` protocol** — a lower-level abstraction used by `ReviewedMRTracker` but not by `ProcessedIssueTracker`

Each had its own keying scheme, caching strategy, and error handling. The Azure Table backends had subtly different failure modes (reviewed-MR tracker failed open on auth errors; issue tracker raised). Note deduplication for discussion interactions didn't exist at all — it was added ad-hoc in webhook/poller code.

## Options Considered

### Option A: Keep separate trackers, add note tracker

Add a third tracker class for note deduplication.

- Pros: Minimal change to existing code
- Cons: Three classes with duplicated caching/backend logic; inconsistent error handling

### Option B: Unified DeduplicationService

One service class with typed methods (`is_review_seen`/`mark_review`, `is_note_seen`/`mark_note`, `is_issue_seen`/`mark_issue`) backed by a single `DeduplicationStore` protocol implementation.

- Pros: Single API surface, consistent error handling, local cache for all key types, one backend to configure
- Cons: Migration requires updating all callers

## Decision

**Option B** — `DeduplicationService` in `dedup.py`.

The service provides six methods (is/mark × review/note/issue) with consistent behavior:
- **Local `MemoryDedup` front-cache** for fast repeated checks within a process
- **Shared backend** (`AzureTableDedup` or `MemoryDedup`) for cross-instance visibility
- **Fail-open on auth/permission errors** — returns `seen=True` to prevent duplicate processing when the backend is inaccessible
- **Fail-safe on transient errors** — returns `seen=False` to allow retry
- **Review key strategy** — configurable via `review_on_push`: when true, keys on `(project, sha)` to deduplicate across MRs sharing a head commit; when false, keys on `(project, mr_iid, sha)`
- **Issue dedup is local-only** — run-scoped, cleared on registry reload

After unification, `ReviewedMRTracker` and `ProcessedIssueTracker` were deleted.

## Consequences

- Single `DeduplicationService` instance created in `main.py` lifespan and stored in `AppContext`
- All callers (`gitlab_webhook.py`, `gitlab_poller.py`, `jira_poller.py`) use the same service
- Note deduplication added as a first-class operation (prevents duplicate webhook deliveries)
- Backend is selected at startup based on Azure Storage configuration
- `aclose()` method for graceful shutdown of backend connections
