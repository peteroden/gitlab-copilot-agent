# 0017. End-to-End Tracing with W3C Traceparent Across Queue Boundary

## Status

Accepted

## Context

The service uses Azure Storage Queue to decouple webhook/poller triggers from task execution. Without trace context propagation, a single user action (e.g., MR push → review) produces two disconnected traces: one in the API process and one in the task runner. This makes it impossible to correlate latency, errors, or audit events across the full request lifecycle.

OpenTelemetry provides `propagate.inject()/extract()` for serializing W3C `traceparent` and `tracestate` headers into arbitrary carriers. The queue message payload is the natural carrier for cross-process propagation.

## Options Considered

### Option A: Manual traceparent serialization

Serialize `traceparent` string manually using `trace.get_current_span().get_span_context()`, pass as a queue message field, and reconstruct on the consumer side.

- Pros: No dependency on propagation API
- Cons: Ignores `tracestate`; reimplements what the SDK already provides; fragile format handling

### Option B: OpenTelemetry propagation API with dict carrier

Use `propagate.inject(carrier)` to serialize trace context into a dict, include that dict in the queue message payload (`QueueTaskPayload`), and use `propagate.extract(carrier)` on the consumer side to restore context.

- Pros: Standard W3C format; propagates `tracestate`; forward-compatible with new propagators; two API calls total
- Cons: Adds OpenTelemetry propagation dependency (already present via the tracing package)

### Option C: Out-of-band trace ID correlation

Pass only a trace ID string; consumer starts a new trace and logs the original ID as a link.

- Pros: Simple
- Cons: Traces are not truly connected; span parent-child relationships are lost; tooling shows separate traces

## Decision

**Option B** — OpenTelemetry propagation API with dict carrier.

The implementation:

1. **Producer** (`remote_executor.py`): `propagate.inject()` writes `traceparent` and `tracestate` into the `QueueTaskPayload` Pydantic model before queue submission.
2. **Consumer** (`task_runner.py`): `restore_trace_context()` calls `propagate.extract()` on the payload fields and attaches the restored context via `context.attach()`. All subsequent spans become children of the original trace.
3. **Cleanup**: `_detach_trace()` detaches the restored context in all exit paths (success, error, finally) to prevent context leakage.
4. **Pipeline spans**: `run_pipeline()` accepts `span_attributes` for semantic attributes (`project_id`, `mr_iid`, `task_type`, `trigger_source`) on the parent pipeline span.

### Trust boundary

The queue is an internal-only transport (Azure Storage Queue with SAS auth). Trace context from the queue is trusted because only authenticated producers can enqueue messages. If the queue boundary ever becomes external-facing, trace context should be validated or replaced with trace links instead of parent-child relationships.

## Consequences

- A single trace spans the full lifecycle: webhook/poller → queue → task runner → pipeline stages
- `tracestate` propagation supports vendor-specific context (e.g., sampling decisions)
- OTLP export (when `OTEL_EXPORTER_OTLP_ENDPOINT` is set) sends connected traces to any compatible backend
- Console collector (`scripts/otel_console_collector.py`) can display full traces for local development
- Semantic span attributes on pipeline spans enable filtering/grouping by project, MR, task type, and trigger source in trace backends
