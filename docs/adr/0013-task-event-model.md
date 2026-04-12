# 0013. Internal TaskEvent Model Replacing Webhook Payload Synthesis

## Status

Accepted

## Context

The service had three trigger sources (webhook, GitLab poller, Jira poller) that all needed to invoke the same review/discussion pipelines. The original design used `MergeRequestWebhookPayload` and `NoteWebhookPayload` (Pydantic models of the GitLab webhook JSON) as the internal contract. This forced non-webhook triggers to synthesize fake webhook payloads:

1. **GitLab poller** constructed `MergeRequestWebhookPayload` objects from `MRListItem` API responses, mapping fields like `web_url` → `git_http_url`, `sha` → `last_commit.id`
2. **Discussion note scanning** constructed `NoteWebhookPayload` objects from discussion API data
3. **Field mismatches** — poller-synthesized payloads left webhook-specific fields (like `action`, `oldrev`) as dummy values since they had no meaning outside webhooks
4. **Tight coupling** — any change to webhook payload parsing required updating poller synthesis code

The research analysis identified this as a key coupling point: "pollers synthesize webhook payloads to reuse webhook-oriented orchestrators."

## Options Considered

### Option A: Keep webhook payloads as internal contract

Add optional fields and factory methods for non-webhook sources.

- Pros: No new model, smaller diff
- Cons: Perpetuates the semantic mismatch; payload grows with fields that only one trigger uses

### Option B: Internal TaskEvent model

Define a `TaskEvent` Pydantic model with exactly the fields needed by pipelines, produced by all trigger sources.

- Pros: Clean contract, each trigger maps its own data into the shared model, no dummy fields
- Cons: New model to maintain; migration touches every trigger and every pipeline

## Decision

**Option B** — `TaskEvent` as the internal event contract.

`TaskEvent` is a frozen Pydantic model (`ConfigDict(frozen=True)`) in `events.py` with fields: `task_type`, `project_id`, `repo`, `clone_url`, `branch`, `target_branch`, `mr_iid`, `head_sha`, `trigger_source`, `token`, `credential_ref`, `resolution_behavior`, `note_id`, `discussion_id`, `note_body`.

Key design choices:
- **Token security**: `token` uses `Field(exclude=True, repr=False)` so it's excluded from serialization and `repr()`. A `log_safe()` method returns all fields except token for structured logging.
- **Per-task-type validation**: `model_validator` enforces that discussion tasks have `note_id` and `discussion_id`, review tasks have `head_sha`, etc.
- **Trigger-agnostic**: `trigger_source` is metadata for logging/tracing; pipelines don't branch on it.

## Consequences

- Webhook, GitLab poller, and Jira poller each construct `TaskEvent` from their own data shapes
- Pipelines receive `TaskEvent` instead of webhook payload models
- No more `MergeRequestWebhookPayload` synthesis in pollers
- Token is never accidentally logged or serialized
- `ScheduledTask` (for queue-based dispatch) wraps `TaskEvent` for serialization across the queue boundary
