# 0009. SHA Marker for Incremental MR Review State

## Status

Accepted

## Context

Feature 5 (incremental review) needs to know the SHA of the last-reviewed commit to compute an incremental diff. Three mechanisms were evaluated: `oldrev` from webhook payloads, in-memory state on the poller, and a hidden SHA marker embedded in GitLab overview notes.

## Options Considered

### Option A: `oldrev` from webhook + in-memory poller dict
- Pros: `oldrev` is directly available on webhook update events; no GitLab state needed for webhook path.
- Cons: `oldrev` reflects last push, not last review (skipped reviews produce wrong diffs). Poller path has no `oldrev` — the GitLab MR list API doesn't return it. In-memory dict is lost on restart.

### Option B: SHA marker in GitLab overview notes
- Pros: Tracks actual last-reviewed SHA. Survives restarts and deploys. Works identically for webhook and poller paths. Self-healing — absent marker triggers full review.
- Cons: Requires parsing overview notes. Depends on summary note being posted successfully.

### Option C: Extend DeduplicationStore protocol with value retrieval
- Pros: Reuses existing infrastructure.
- Cons: Requires protocol change across all backends (MemoryDedup, Azure). Boolean-only design is intentional for simplicity. Over-engineered for this use case.

## Decision

Option B. Embed `<!-- mr-review-agent: last_reviewed_sha={sha} -->` in the summary note posted after each review. Extract via regex on subsequent reviews. When no marker is found (first review, missed review, post-deploy), fall back to full MR diff — which is the correct behavior for those cases.

The diff selection logic uses marker presence as the single decision point, regardless of webhook action type (open/update/reopen). This eliminates action-based branching and makes the system self-healing.

## Consequences

- Positive: Works for both webhook and poller paths with zero poller changes.
- Positive: Self-healing — missed reviews automatically get full coverage on the next trigger.
- Positive: Simplifies orchestrator logic to one branch (marker found vs not found).
- Negative: First review after Feature 5 deployment always does a full diff (no pre-existing markers). This is acceptable and correct.
- Negative: If summary note posting fails, next review falls back to full diff. Acceptable degradation.
