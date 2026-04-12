"""Minimal OTEL collector that logs received telemetry to the console.

Usage:
    uv run python scripts/otel_console_collector.py [--port 4317] [--verbose]

Starts a gRPC server on port 4317 that accepts OTLP traces, metrics,
and logs, printing a summary line for each batch. Useful for local
development and E2E testing without a real collector.

Pass ``--verbose`` to print per-span detail including trace IDs, parent
linkage, and attributes — useful for verifying trace propagation.
"""

from __future__ import annotations

import argparse
from concurrent import futures
from datetime import UTC, datetime

import grpc
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1 import (
    logs_service_pb2,
    logs_service_pb2_grpc,
)
from opentelemetry.proto.collector.metrics.v1 import (
    metrics_service_pb2,
    metrics_service_pb2_grpc,
)
from opentelemetry.proto.collector.trace.v1 import (
    trace_service_pb2,
    trace_service_pb2_grpc,
)

_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_RESET = "\033[0m"


# Global verbose flag — set from CLI args before servers start.
_verbose = False


def _ts() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]


def _resource_name(resource_spans_or_metrics: object) -> str:
    """Extract service.name from resource attributes."""
    try:
        d = MessageToDict(resource_spans_or_metrics)  # type: ignore[arg-type]
        for attr in d.get("resource", {}).get("attributes", []):
            if attr.get("key") == "service.name":
                return attr["value"].get("stringValue", "unknown")
    except Exception:
        pass
    return "unknown"


def _format_attr_value(val: object) -> str:
    """Format an OTLP AnyValue proto to a compact string."""
    for field in ("string_value", "int_value", "double_value", "bool_value"):
        v = getattr(val, field, None)
        if v is not None and v != "" and v != 0:
            return str(v)
    # Check explicitly for 0/False/empty which getattr misses
    if getattr(val, "int_value", None) == 0 and val.HasField("int_value"):  # type: ignore[union-attr]
        return "0"
    if getattr(val, "bool_value", None) is False and val.HasField("bool_value"):  # type: ignore[union-attr]
        return "false"
    return "?"


def _format_span_attrs(span: object, limit: int = 6) -> str:
    """Format span attributes as compact key=value pairs."""
    attrs = getattr(span, "attributes", [])
    parts = []
    for attr in attrs[:limit]:
        parts.append(f"{attr.key}={_format_attr_value(attr.value)}")
    if len(attrs) > limit:
        parts.append(f"+{len(attrs) - limit} more")
    return ", ".join(parts)


def _verbose_json_traces(body: bytes) -> None:
    """Print per-span detail from raw OTLP JSON (avoids protobuf base64 ID mangling)."""
    import json as json_mod  # noqa: PLC0415

    try:
        data = json_mod.loads(body)
    except (json_mod.JSONDecodeError, ValueError):
        return
    for rs in data.get("resourceSpans", []):
        svc = "unknown"
        for attr in rs.get("resource", {}).get("attributes", []):
            if attr.get("key") == "service.name":
                svc = attr.get("value", {}).get("stringValue", "unknown")
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                tid = s.get("traceId", "?")
                sid = s.get("spanId", "?")
                pid = s.get("parentSpanId") or "-"
                name = s.get("name", "<unnamed>")
                start = int(s.get("startTimeUnixNano", 0))
                end = int(s.get("endTimeUnixNano", 0))
                dur_ms = (end - start) / 1e6
                attrs_list = s.get("attributes", [])
                attr_parts = []
                for a in attrs_list[:6]:
                    val = a.get("value", {})
                    v = (
                        val.get("stringValue")
                        or val.get("intValue")
                        or val.get("doubleValue")
                        or val.get("boolValue")
                        or "?"
                    )
                    attr_parts.append(f"{a['key']}={v}")
                if len(attrs_list) > 6:
                    attr_parts.append(f"+{len(attrs_list) - 6} more")
                attrs_str = ", ".join(attr_parts)
                print(
                    f"{_DIM}[{_ts()}]{_RESET} {_CYAN}SPAN{_RESET}   "
                    f"trace={tid} span={sid} parent={pid} "
                    f"name={name}  svc={svc}  {dur_ms:.1f}ms"
                )
                if attrs_str:
                    print(f"                  attrs: {attrs_str}")


class TraceService(trace_service_pb2_grpc.TraceServiceServicer):
    def Export(self, request, context, *, _skip_verbose: bool = False):  # type: ignore[override]
        for rs in request.resource_spans:
            svc = _resource_name(rs)
            names = []
            for ss in rs.scope_spans:
                for s in ss.spans:
                    names.append(s.name)
                    if _verbose and not _skip_verbose:
                        tid = s.trace_id.hex()
                        sid = s.span_id.hex()
                        pid = s.parent_span_id.hex() if s.parent_span_id else "-"
                        dur_ms = (s.end_time_unix_nano - s.start_time_unix_nano) / 1e6
                        attrs = _format_span_attrs(s)
                        print(
                            f"{_DIM}[{_ts()}]{_RESET} {_CYAN}SPAN{_RESET}   "
                            f"trace={tid} span={sid} parent={pid} "
                            f"name={s.name}  svc={svc}  {dur_ms:.1f}ms"
                        )
                        if attrs:
                            print(f"                  attrs: {attrs}")
            span_count = len(names)
            preview = ", ".join(names[:5])
            if len(names) > 5:
                preview += f" (+{len(names) - 5} more)"
            print(f"{_CYAN}[{_ts()}] TRACE{_RESET}  svc={svc}  spans={span_count}  [{preview}]")
        return trace_service_pb2.ExportTraceServiceResponse()


class MetricsService(metrics_service_pb2_grpc.MetricsServiceServicer):
    def Export(self, request, context):  # type: ignore[override]
        for rm in request.resource_metrics:
            svc = _resource_name(rm)
            metric_names = []
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    metric_names.append(m.name)
            preview = ", ".join(metric_names[:8])
            if len(metric_names) > 8:
                preview += f" (+{len(metric_names) - 8} more)"
            print(
                f"{_GREEN}[{_ts()}] METRIC{_RESET} "
                f"svc={svc}  metrics={len(metric_names)}  [{preview}]"
            )
        return metrics_service_pb2.ExportMetricsServiceResponse()


class LogsService(logs_service_pb2_grpc.LogsServiceServicer):
    def Export(self, request, context):  # type: ignore[override]
        for rl in request.resource_logs:
            svc = _resource_name(rl)
            for sl in rl.scope_logs:
                for lr in sl.log_records:
                    body = ""
                    if lr.body.string_value:
                        body = lr.body.string_value[:120]
                    severity = lr.severity_text or "?"
                    print(f"{_YELLOW}[{_ts()}] LOG{_RESET}    svc={svc}  level={severity}  {body}")
        return logs_service_pb2.ExportLogsServiceResponse()


def _run_http_server(port: int) -> None:
    """Run a simple HTTP OTLP receiver alongside the gRPC server.

    Handles POST to /v1/traces, /v1/metrics, /v1/logs with protobuf or JSON
    bodies (auto-detected via Content-Type). The Copilot CLI uses OTLP HTTP
    with JSON encoding by default (port 4318).
    """
    import http.server  # noqa: PLC0415
    import json as json_mod  # noqa: PLC0415
    import threading  # noqa: PLC0415

    from google.protobuf.json_format import Parse  # noqa: PLC0415
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (  # noqa: PLC0415
        ExportLogsServiceRequest,
    )
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (  # noqa: PLC0415
        ExportMetricsServiceRequest,
    )
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: PLC0415
        ExportTraceServiceRequest,
    )

    trace_svc = TraceService()
    metrics_svc = MetricsService()
    logs_svc = LogsService()

    _ROUTE_MAP: dict[str, tuple[type, object]] = {
        "/v1/traces": (ExportTraceServiceRequest, trace_svc),
        "/v1/metrics": (ExportMetricsServiceRequest, metrics_svc),
        "/v1/logs": (ExportLogsServiceRequest, logs_svc),
    }

    class OTLPHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # type: ignore[override]  # noqa: A002
            pass  # suppress access logs

        def do_POST(self):  # noqa: N802
            te = self.headers.get("Transfer-Encoding", "")
            if "chunked" in te:
                body = self._read_chunked()
            else:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
            content_type = self.headers.get("Content-Type", "")
            is_json = "json" in content_type
            if _verbose:
                print(
                    f"{_DIM}[{_ts()}]{_RESET} HTTP   "
                    f"path={self.path}  len={len(body)}  type={'json' if is_json else 'proto'}"
                )
            try:
                route = _ROUTE_MAP.get(self.path)
                if route and body:
                    msg_cls, svc = route
                    # For verbose JSON traces, use raw JSON to avoid
                    # protobuf base64 mangling of trace/span IDs.
                    if _verbose and is_json and self.path == "/v1/traces":
                        _verbose_json_traces(body)
                    req = msg_cls()
                    if is_json:
                        Parse(body, req, ignore_unknown_fields=True)
                    else:
                        req.ParseFromString(body)
                    # Skip proto-level verbose for JSON traces (already displayed above)
                    svc.Export(req, None, _skip_verbose=is_json)  # type: ignore[union-attr]
                self.send_response(200)
                self.end_headers()
            except Exception as exc:
                print(f"HTTP parse error on {self.path}: {exc}")
                self.send_response(400)
                self.end_headers()

        def _read_chunked(self) -> bytes:
            chunks: list[bytes] = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                size = int(line, 16)
                if size == 0:
                    self.rfile.readline()  # trailing CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()  # chunk-terminating CRLF
            return b"".join(chunks)

    httpd = http.server.HTTPServer(("0.0.0.0", port), OTLPHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()


def main() -> None:
    global _verbose  # noqa: PLW0603

    parser = argparse.ArgumentParser(description="Console OTEL collector")
    parser.add_argument("--port", type=int, default=4317, help="gRPC listen port")
    parser.add_argument("--http-port", type=int, default=4318, help="HTTP/protobuf listen port")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print per-span detail (trace ID, parent, attrs) for trace propagation debugging",
    )
    args = parser.parse_args()
    _verbose = args.verbose

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(TraceService(), server)
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(MetricsService(), server)
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(LogsService(), server)
    server.add_insecure_port(f"0.0.0.0:{args.port}")
    server.start()

    _run_http_server(args.http_port)

    print(
        f"\n📡 Console OTEL Collector\n"
        f"   gRPC  :{args.port}  (app traces/metrics/logs)\n"
        f"   HTTP  :{args.http_port}  (Copilot CLI traces)\n"
        f"   Set OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:{args.port}\n"
    )
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.stop(grace=2)


if __name__ == "__main__":
    main()
