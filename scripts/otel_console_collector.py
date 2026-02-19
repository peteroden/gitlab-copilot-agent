"""Minimal OTEL collector that logs received telemetry to the console.

Usage:
    uv run python scripts/otel_console_collector.py [--port 4317]

Starts a gRPC server on port 4317 that accepts OTLP traces, metrics,
and logs, printing a summary line for each batch. Useful for local
development and E2E testing without a real collector.
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
_RESET = "\033[0m"


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


class TraceService(trace_service_pb2_grpc.TraceServiceServicer):
    def Export(self, request, context):  # type: ignore[override]
        for rs in request.resource_spans:
            svc = _resource_name(rs)
            span_count = sum(len(ss.spans) for ss in rs.scope_spans)
            names = []
            for ss in rs.scope_spans:
                for s in ss.spans:
                    names.append(s.name)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Console OTEL collector")
    parser.add_argument("--port", type=int, default=4317, help="gRPC listen port")
    args = parser.parse_args()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(TraceService(), server)
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(MetricsService(), server)
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(LogsService(), server)
    server.add_insecure_port(f"0.0.0.0:{args.port}")
    server.start()

    print(
        f"\n📡 Console OTEL Collector listening on :{args.port}\n"
        f"   Set OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:{args.port}\n"
    )
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.stop(grace=2)


if __name__ == "__main__":
    main()
