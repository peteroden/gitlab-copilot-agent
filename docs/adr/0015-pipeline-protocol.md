# 0015. Pipeline Protocol with Stage-Based Execution

## Status

Accepted — Partially supersedes ADR-0001 (flow design)

## Context

The service had three task types (MR review, discussion interaction, Jira coding) each implemented as monolithic async functions:

1. **`handle_review()`** in `orchestrator.py` — 188 lines: clone, fetch MR details, build diff, run LLM, parse comments, post review, cleanup
2. **`handle_discussion_interaction()`** in `discussion_orchestrator.py` — 196 lines: clone, fetch threads, build prompt, run LLM, apply patch, post reply, cleanup
3. **`CodingOrchestrator.handle()`** in `coding_orchestrator.py` — 155 lines: clone, branch, run LLM, apply patch, commit, push, create MR, cleanup

Each function mixed resource lifecycle (clone/cleanup), business logic (LLM invocation, comment parsing), and error handling (post user-visible errors, record metrics) in a single scope. Cleanup was inconsistent — some functions used try/finally, others relied on callers.

## Options Considered

### Option A: Refactor monoliths into smaller functions

Break each handler into helper functions called sequentially.

- Pros: Simple, no new abstractions
- Cons: Cleanup guarantees still depend on caller discipline; no shared contract; tracing requires per-function instrumentation

### Option B: Pipeline protocol with four-stage contract

Define a `Pipeline` protocol with typed stages (`prepare → execute → process → cleanup`) and a generic runner that enforces ordering, tracing, and cleanup.

- Pros: Consistent contract across all task types; cleanup always runs; per-stage tracing; error handling via `handle_error` callback; testable stages
- Cons: New abstraction layer; each task type must restructure into four stages

### Option C: Actor/message-based pipeline

Use an actor framework or message queue between stages.

- Pros: Decoupled stages, parallelizable
- Cons: Massive over-engineering for three task types; adds async coordination complexity

## Decision

**Option B** — `Pipeline` protocol in `pipeline.py` with `run_pipeline()` runner.

The protocol defines five methods: `prepare`, `execute`, `process`, `cleanup`, `handle_error`. The generic `run_pipeline()` function:

1. Calls `prepare → execute → process` in sequence, each wrapped in a trace span
2. On success, sets `outcome = "success"` (unless the pipeline set a specific outcome like `no_changes`)
3. On failure, calls `handle_error` (which posts user-visible error messages), then `cleanup`
4. On success, calls `cleanup` after process
5. **Cleanup always runs** — even on error, even if handle_error itself fails
6. `suppress_exception` flag allows pipelines to handle errors gracefully without re-raising (used by `CodingPipeline` for transient clone failures)

Three pipeline implementations:
- `ReviewPipeline` — clone, fetch MR+discussions+commits, run review LLM, post comments
- `DiscussionPipeline` — clone, fetch threads, run discussion LLM, optionally apply+push, post reply
- `CodingPipeline` — clone, branch, run coding LLM, apply+commit+push, create MR, update Jira

Each pipeline uses `BasePipelineContext` (Pydantic model) to thread mutable state between stages.

## Consequences

- All three task types follow the same four-stage contract
- `run_pipeline()` is the single entry point — callers don't manage stage ordering or cleanup
- Per-stage trace spans provide consistent observability across task types
- `handle_error` implementations own user-facing error messages (MR comment, Jira comment, thread reply)
- Orchestrator modules (`orchestrator.py`, `discussion_orchestrator.py`, `coding_orchestrator.py`) were reduced to thin wrappers, then eliminated entirely in Phase 6.2
- `stage_requires()` helper enforces inter-stage data contracts at runtime
