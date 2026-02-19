# Observability

OTEL setup, all 7 metrics, structured logging, trace correlation, Helm OTEL Collector configuration.

---

## OpenTelemetry Setup

### Initialization

**Location**: `telemetry.py` → `init_telemetry()`

**Gated By**: `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable

**Behavior**:
- If `OTEL_EXPORTER_OTLP_ENDPOINT` unset → no-op (telemetry disabled)
- If set → configure TracerProvider, MeterProvider, LoggerProvider

**Called From**: `main.py` → `lifespan()` (on startup)

**Shutdown**: `shutdown_telemetry()` called in lifespan cleanup (flushes buffers)

---

### Providers

#### TracerProvider

**Exporter**: OTLPSpanExporter (gRPC)

**Processor**: BatchSpanProcessor (batches spans before export)

**Resource Attributes**:
- `service.name`: `"gitlab-copilot-agent"`
- `service.version`: `$SERVICE_VERSION` (default: `"0.1.0"`)
- `deployment.environment`: `$DEPLOYMENT_ENV` (e.g., `"production"`)

**Auto-Instrumentation**:
- FastAPI: `FastAPIInstrumentor.instrument_app(app)` in `main.py`
- HTTPX: `HTTPXClientInstrumentor().instrument()` in `init_telemetry()`

---

#### MeterProvider

**Exporter**: OTLPMetricExporter (gRPC)

**Reader**: PeriodicExportingMetricReader (exports metrics periodically)

**Resource Attributes**: Same as TracerProvider

**Instruments**: See [All Metrics](#all-metrics) below

---

#### LoggerProvider

**Exporter**: OTLPLogExporter (gRPC)

**Processor**: BatchLogRecordProcessor

**Handler**: `LoggingHandler` attached to `logging.getLogger("gitlab-copilot-agent")`

**Integration**: structlog processor `emit_to_otel_logs` re-emits logs to stdlib logging

---

## All Metrics

**Location**: `metrics.py`

**Meter Name**: `"gitlab_copilot_agent"`

---

### 1. reviews_total

**Type**: Counter

**Unit**: `"1"` (count)

**Description**: Total MR reviews processed

**Labels**:
- `outcome`: `"success"` or `"error"`

**Emitted**: `orchestrator.py` → `handle_review()` (finally block)

**Example**:
```python
reviews_total.add(1, {"outcome": "success"})
```

---

### 2. reviews_duration_seconds

**Type**: Histogram

**Unit**: `"s"` (seconds)

**Description**: Duration of MR review processing (end-to-end: clone → review → post)

**Labels**:
- `outcome`: `"success"` or `"error"`

**Emitted**: `orchestrator.py` → `handle_review()` (finally block)

**Buckets**: Default OTEL histogram buckets

**Example**:
```python
reviews_duration.record(elapsed, {"outcome": "success"})
```

---

### 3. coding_tasks_total

**Type**: Counter

**Unit**: `"1"` (count)

**Description**: Total coding tasks processed (Jira issues)

**Labels**:
- `outcome`: `"success"`, `"no_changes"`, or `"error"`

**Emitted**: `coding_orchestrator.py` → `CodingOrchestrator.handle()` (finally block)

**Example**:
```python
coding_tasks_total.add(1, {"outcome": "success"})
```

---

### 4. coding_tasks_duration_seconds

**Type**: Histogram

**Unit**: `"s"` (seconds)

**Description**: Duration of coding task processing (Jira issue → MR creation)

**Labels**:
- `outcome`: `"success"`, `"no_changes"`, or `"error"`

**Emitted**: `coding_orchestrator.py` → `CodingOrchestrator.handle()` (finally block)

**Example**:
```python
coding_tasks_duration.record(elapsed, {"outcome": "success"})
```

---

### 5. webhook_received_total

**Type**: Counter

**Unit**: `"1"` (count)

**Description**: Total webhooks received (all event types)

**Labels**:
- `object_kind`: `"merge_request"`, `"note"`, or `"unknown"`

**Emitted**: `webhook.py` → `webhook()` endpoint (before payload parsing)

**Example**:
```python
webhook_received_total.add(1, {"object_kind": "merge_request"})
```

---

### 6. webhook_errors_total

**Type**: Counter

**Unit**: `"1"` (count)

**Description**: Webhook background processing errors

**Labels**:
- `handler`: `"review"` or `"copilot_comment"`

**Emitted**: `webhook.py` → `_process_review()` or `_process_copilot_comment()` (exception handler)

**Example**:
```python
webhook_errors_total.add(1, {"handler": "review"})
```

---

### 7. copilot_session_duration_seconds

**Type**: Histogram

**Unit**: `"s"` (seconds)

**Description**: Duration of Copilot SDK session (prompt → model → result)

**Labels**:
- `task_type`: `"review"` or `"coding"`

**Emitted**: `copilot_session.py` → `run_copilot_session()` (finally block)

**Example**:
```python
copilot_session_duration.record(elapsed, {"task_type": "review"})
```

---

## Structured Logging

### Configuration

**Location**: `main.py`

**Library**: structlog

**Processors**:
1. `structlog.contextvars.merge_contextvars` — Include context vars
2. `structlog.stdlib.add_log_level` — Add log level
3. `structlog.processors.TimeStamper(fmt="iso")` — Add ISO timestamp
4. `add_trace_context` — Inject trace_id, span_id from active span
5. `emit_to_otel_logs` — Re-emit to stdlib logging for OTLP export
6. `structlog.processors.format_exc_info` — Format exceptions
7. `structlog.dev.ConsoleRenderer()` — Human-readable console output

---

### Log Format

**Console** (dev):
```
2025-02-19T12:34:56.789Z [info] review_started project_id=42 mr_iid=7 trace_id=abc123 span_id=def456
```

**OTLP** (production):
- JSON structured logs exported to OTLP endpoint
- Trace context automatically correlated by SDK

---

### Logging Patterns

**Bind Context**:
```python
bound_log = log.bind(project_id=project.id, mr_iid=mr.iid)
await bound_log.ainfo("review_started")
await bound_log.ainfo("review_complete", inline_comments=len(parsed.comments))
```

**Exception Logging**:
```python
await bound_log.aexception("review_failed")
# Automatically includes exception type, message, traceback
```

**Levels**:
- `adebug()`: Debug info (not exported to OTLP by default)
- `ainfo()`: Info (normal operations)
- `awarn()`: Warning (unexpected but recoverable)
- `aerror()`: Error (operation failed)
- `aexception()`: Error with exception info

---

## Trace Correlation

### Span Creation

**Manual Spans**:
```python
from gitlab_copilot_agent.telemetry import get_tracer

_tracer = get_tracer(__name__)

with _tracer.start_as_current_span("mr.review", attributes={"project_id": project.id}):
    # Operation
    pass
```

**Auto-Instrumentation**:
- FastAPI: HTTP request spans created automatically
- HTTPX: HTTP client spans for GitLab/Jira API calls

---

### Trace Context in Logs

**Processor**: `telemetry.py` → `add_trace_context()`

**Behavior**:
1. Get current span from context
2. Extract trace_id and span_id
3. Add to log event dict as `trace_id`, `span_id`

**Format**: Hex strings (e.g., `trace_id="abc123..."`, `span_id="def456..."`)

**Usage**: Correlate logs to traces in observability platform (e.g., Jaeger, Tempo)

---

### Span Hierarchy

**Example**: MR Review

```
http.request (FastAPI)
└── mr.review (orchestrator.py)
    ├── git.clone (git_operations.py)
    ├── copilot.session (copilot_session.py)
    └── git.cleanup (git_operations.py)
```

**Attributes**: Each span includes relevant context (project_id, mr_iid, repo_path, etc.)

---

## Console Collector Script (Local Dev)

**Purpose**: Run local OTEL Collector for dev/test without K8s cluster.

**Script**: `scripts/otel-console-collector.sh` (not in repo, example below)

**Example**:
```bash
#!/bin/bash
# Run OTEL Collector in Docker with console exporter

docker run --rm -p 4317:4317 -p 4318:4318 \
  -v $(pwd)/otel-config.yaml:/etc/otel/config.yaml \
  otel/opentelemetry-collector-contrib:0.115.0 \
  --config /etc/otel/config.yaml
```

**otel-config.yaml**:
```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

exporters:
  logging:
    loglevel: debug

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [logging]
    metrics:
      receivers: [otlp]
      exporters: [logging]
    logs:
      receivers: [otlp]
      exporters: [logging]
```

**Usage**:
```bash
# Terminal 1: Run collector
./scripts/otel-console-collector.sh

# Terminal 2: Run agent with OTEL enabled
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
uv run uvicorn gitlab_copilot_agent.main:app --host 0.0.0.0 --port 8000
```

**Output**: Traces, metrics, logs printed to console (logging exporter)

---

## Helm OTEL Collector DaemonSet

**Template**: `helm/gitlab-copilot-agent/templates/otel-collector.yaml`

**Purpose**: Deploy OTEL Collector as DaemonSet to collect telemetry from all nodes.

**Enabled**: Only if `telemetry.otlpEndpoint` is set in values.yaml

**ConfigMap**: OTEL Collector config (receivers, exporters, pipelines)

**DaemonSet**:
- Runs on every node
- Receives OTLP gRPC on port 4317
- Exports to configured backend (e.g., Jaeger, Tempo, Prometheus)

**Service**: ClusterIP service `otel-collector:4317`

**Agent Config**: Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317`

---

### OTEL Collector Config

**Location**: `templates/otel-collector.yaml` → ConfigMap

**Receivers**:
```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
```

**Exporters** (example):
```yaml
exporters:
  prometheus:
    endpoint: "0.0.0.0:8889"
  
  jaeger:
    endpoint: jaeger-collector:14250
    tls:
      insecure: true
  
  loki:
    endpoint: http://loki:3100/loki/api/v1/push
```

**Pipelines**:
```yaml
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [jaeger]
    
    metrics:
      receivers: [otlp]
      exporters: [prometheus]
    
    logs:
      receivers: [otlp]
      exporters: [loki]
```

---

### Example: Prometheus + Grafana

**Install Prometheus Operator**:
```bash
helm install prometheus prometheus-community/kube-prometheus-stack -n monitoring
```

**Configure OTEL Collector**:
```yaml
telemetry:
  otlpEndpoint: http://otel-collector:4317
```

**ServiceMonitor** (scrape OTEL Collector metrics):
```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: otel-collector
spec:
  selector:
    matchLabels:
      app: otel-collector
  endpoints:
  - port: prometheus
    interval: 30s
```

**Grafana Dashboard**: Import dashboard for `gitlab_copilot_agent` metrics

---

## Dashboards & Alerts

### Grafana Dashboard Example

**Panels**:
1. **Review Throughput**: `rate(reviews_total[5m])` grouped by outcome
2. **Review Latency**: `histogram_quantile(0.95, reviews_duration_seconds)` (p95)
3. **Webhook Error Rate**: `rate(webhook_errors_total[5m])` grouped by handler
4. **Copilot Session Duration**: `histogram_quantile(0.95, copilot_session_duration_seconds)`
5. **Active Jobs**: `count(kube_job_status_active{job=~"copilot-.*"})`
6. **Redis Connection Status**: `redis_connected_clients`

---

### Prometheus Alerts

**High Error Rate**:
```yaml
- alert: HighWebhookErrorRate
  expr: rate(webhook_errors_total[5m]) > 0.1
  for: 5m
  annotations:
    summary: "High webhook error rate ({{ $value }} errors/sec)"
```

**Slow Reviews**:
```yaml
- alert: SlowReviews
  expr: histogram_quantile(0.95, reviews_duration_seconds) > 180
  for: 10m
  annotations:
    summary: "Review p95 latency > 3 minutes"
```

**Job Failures**:
```yaml
- alert: JobFailures
  expr: kube_job_status_failed{job=~"copilot-.*"} > 0
  for: 1m
  annotations:
    summary: "K8s Job failed: {{ $labels.job }}"
```

---

## Debugging with Telemetry

### Trace a Review

1. **Trigger Review**: Send webhook or let poller discover MR
2. **Find Trace ID**: Check logs for `trace_id` field
3. **Open Jaeger/Tempo**: Search by trace ID
4. **View Span Tree**: See timing breakdown (clone, review, post)
5. **Check Errors**: Failed spans highlighted

**Example Log**:
```
[info] review_started project_id=42 mr_iid=7 trace_id=abc123 span_id=def456
```

**Jaeger Query**:
```
trace_id=abc123
```

---

### Correlate Logs to Traces

**Loki Query** (if using Loki):
```
{app="gitlab-copilot-agent"} |= "trace_id=abc123"
```

**Result**: All logs from that trace (review_started, review_complete, etc.)

---

### Metrics Investigation

**Review Latency Spike**:
1. Check `reviews_duration_seconds` histogram
2. Identify p95/p99 spike
3. Find slow traces (filter by duration in Jaeger)
4. Inspect spans: Is SDK slow? Clone? Post?
5. Check logs for errors during spike

**Example PromQL**:
```promql
histogram_quantile(0.95, reviews_duration_seconds)
```

---

## Local Development

**Disable Telemetry**:
```bash
# Unset OTEL_EXPORTER_OTLP_ENDPOINT
unset OTEL_EXPORTER_OTLP_ENDPOINT
uv run uvicorn gitlab_copilot_agent.main:app
```

**Console Collector**:
```bash
# Terminal 1: Run collector
docker run --rm -p 4317:4317 \
  otel/opentelemetry-collector-contrib:0.115.0 \
  --config /tmp/otel-config.yaml

# Terminal 2: Run agent
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
uv run uvicorn gitlab_copilot_agent.main:app
```

**View Traces**: Console output from collector (logging exporter)

---

## Production Configuration

**Recommendation**: Use managed observability platform (Datadog, Honeycomb, New Relic, etc.)

**Example (Datadog)**:
```yaml
telemetry:
  otlpEndpoint: http://datadog-agent:4317
  environment: production
```

**Datadog Agent Config**:
```yaml
# datadog-agent values.yaml
datadog:
  apiKey: <DD_API_KEY>
  site: datadoghq.com
  otlp:
    receiver:
      protocols:
        grpc:
          enabled: true
```

---

## Metric Retention

**OTEL Collector**: No retention (pass-through)

**Prometheus**: Default 15 days (configurable)

**Production**: Use long-term storage (Thanos, Cortex, Mimir)

---

## Cost Optimization

**Sampling**:
- Trace sampling: 10% of traces (reduce storage cost)
- Metric aggregation: Pre-aggregate histograms

**Filtering**:
- Drop debug logs in production
- Filter health check traces

**Example OTEL Collector Config**:
```yaml
processors:
  tail_sampling:
    policies:
    - name: error_traces
      type: status_code
      status_code:
        status_codes: [ERROR]
    - name: probabilistic
      type: probabilistic
      probabilistic:
        sampling_percentage: 10

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [tail_sampling]
      exporters: [jaeger]
```

---

## Troubleshooting

### No Telemetry Data

**Check**:
1. `OTEL_EXPORTER_OTLP_ENDPOINT` set?
2. OTEL Collector reachable? (`curl http://otel-collector:4317`)
3. Logs show telemetry initialization?
4. Collector logs show received data?

---

### High Cardinality Warning

**Cause**: Too many unique label values (e.g., `trace_id` as label)

**Fix**: Use attributes for high-cardinality data, not labels

**Example** (bad):
```python
metric.add(1, {"trace_id": trace_id})  # ❌ High cardinality
```

**Example** (good):
```python
metric.add(1, {"outcome": "success"})  # ✅ Low cardinality
# Trace ID captured as span attribute, not metric label
```

---

## Summary

**Telemetry Stack**:
- **Metrics**: 7 instruments (counters, histograms)
- **Traces**: Manual spans + FastAPI/HTTPX auto-instrumentation
- **Logs**: structlog with OTLP export + trace correlation

**Local Dev**: Console collector (logging exporter)

**Production**: OTEL Collector DaemonSet → Prometheus/Jaeger/Loki

**Key Metrics**: `reviews_total`, `reviews_duration_seconds`, `webhook_errors_total`, `copilot_session_duration_seconds`

**Trace Correlation**: `trace_id` and `span_id` injected into logs via `add_trace_context` processor
