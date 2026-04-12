"""Forward Copilot CLI spans from JSONL file to the app's OTLP exporter.

The Copilot CLI writes spans to a JSONL file via ``TelemetryConfig(file_path=...)``.
After the CLI exits, this module reads the file, converts each span to
``ReadableSpan``, and exports them through the app's existing OTLP exporter —
preserving the original timestamps and trace/span IDs so CLI spans appear as
children of the app's pipeline spans in the trace backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import structlog
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, StatusCode, TraceFlags
from opentelemetry.trace.status import Status

from gitlab_copilot_agent.telemetry import _state

log = structlog.get_logger()

_KIND_MAP = {
    0: SpanKind.INTERNAL,
    1: SpanKind.SERVER,
    2: SpanKind.CLIENT,
    3: SpanKind.PRODUCER,
    4: SpanKind.CONSUMER,
}


def forward_cli_traces(file_path: str) -> int:
    """Read CLI JSONL spans and export via the app's OTLP span exporter.

    Returns the number of spans successfully exported, or 0 if OTEL is not
    initialized or the file is missing/empty.  Never raises — telemetry
    must not fail task execution.
    """
    exporter = _state.span_exporter
    if exporter is None:
        return 0

    path = Path(file_path)
    if not path.exists():
        return 0

    spans: list[ReadableSpan] = []
    for line_no, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if data.get("type") != "span":
                continue
            spans.append(_parse_span(data))
        except Exception:
            log.debug("cli_trace_parse_skip", line=line_no, file=file_path)

    if not spans:
        return 0

    try:
        exporter.export(spans)
    except Exception:
        log.debug("cli_trace_export_failed", count=len(spans))
        return 0

    log.info("cli_traces_forwarded", count=len(spans))
    return len(spans)


def _parse_span(data: dict[str, Any]) -> ReadableSpan:
    """Convert a single CLI JSONL span dict to a ReadableSpan."""
    trace_id = int(data["traceId"], 16)
    span_id = int(data["spanId"], 16)
    parent_id = int(data["parentSpanId"], 16) if data.get("parentSpanId") else 0

    ctx = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(0x01),
    )
    parent = (
        SpanContext(
            trace_id=trace_id,
            span_id=parent_id,
            is_remote=True,
            trace_flags=TraceFlags(0x01),
        )
        if parent_id
        else None
    )

    start_s, start_ns = data["startTime"]
    end_s, end_ns = data["endTime"]

    attrs = _flatten_attributes(data.get("attributes", {}))

    res_attrs = data.get("resource", {}).get("attributes", {})
    resource = Resource.create(res_attrs)

    scope_data = data.get("instrumentationScope", {})
    scope = InstrumentationScope(
        name=scope_data.get("name", "github.copilot"),
        version=scope_data.get("version"),
    )

    status_code = data.get("status", {}).get("code", 0)
    status_msg = data.get("status", {}).get("message", "")

    return ReadableSpan(
        name=data["name"],
        context=ctx,
        parent=parent,
        resource=resource,
        attributes=attrs,
        kind=_KIND_MAP.get(data.get("kind", 0), SpanKind.INTERNAL),
        start_time=start_s * 10**9 + start_ns,
        end_time=end_s * 10**9 + end_ns,
        instrumentation_scope=scope,
        status=Status(
            StatusCode.OK if status_code == 0 else StatusCode.ERROR,
            status_msg,
        ),
    )


def _flatten_attributes(
    raw: dict[str, Any],
) -> dict[str, str | int | float | bool | tuple[str, ...]]:
    """Convert CLI attribute values to OTEL-valid types."""
    result: dict[str, str | int | float | bool | tuple[str, ...]] = {}
    for k, v in raw.items():
        if isinstance(v, (str, int, float, bool)):
            result[k] = v
        elif isinstance(v, list):
            items = cast("list[object]", v)
            strs = [s for s in items if isinstance(s, str)]
            if len(strs) == len(items):
                result[k] = tuple(strs)
    return result
