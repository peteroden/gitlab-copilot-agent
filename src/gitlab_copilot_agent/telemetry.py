"""OpenTelemetry tracing and log export setup."""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = "gitlab-copilot-agent"
_otel_logging_configured = False


def init_telemetry() -> None:
    """Configure OpenTelemetry tracing + log export.

    No-op if OTEL_EXPORTER_OTLP_ENDPOINT is unset.
    """
    global _otel_logging_configured  # noqa: PLW0603
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({"service.name": _SERVICE_NAME})

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    # Logs → OTLP
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)

    handler = LoggingHandler(logger_provider=logger_provider)
    otel_logger = logging.getLogger(_SERVICE_NAME)
    otel_logger.addHandler(handler)
    otel_logger.setLevel(logging.DEBUG)
    _otel_logging_configured = True


def shutdown_telemetry() -> None:
    """Flush and shutdown providers."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()

    if _otel_logging_configured:
        from opentelemetry._logs import get_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider

        log_provider = get_logger_provider()
        if isinstance(log_provider, LoggerProvider):
            log_provider.shutdown()  # type: ignore[no-untyped-call]


def get_tracer(name: str) -> trace.Tracer:
    """Get a tracer instance."""
    return trace.get_tracer(name)


def add_trace_context(logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Structlog processor that injects trace_id and span_id from the active span."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def emit_to_otel_logs(logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Structlog processor that re-emits to stdlib logging for OTel log export.

    The OTel LoggingHandler on the root logger picks up these records and
    exports them via OTLP. Trace context is automatically correlated by the SDK.
    Only active when OTEL_EXPORTER_OTLP_ENDPOINT is configured.
    """
    if not _otel_logging_configured:
        return event_dict

    level_name = event_dict.get("level", "info").upper()
    level = getattr(logging, level_name, logging.INFO)
    msg = event_dict.get("event", "")
    # Filter out keys that conflict with stdlib LogRecord reserved attributes
    _reserved = {"event", "level", "timestamp", "exc_info", "stack_info", "stackLevel"}
    extra = {k: v for k, v in event_dict.items() if k not in _reserved}
    logging.getLogger(_SERVICE_NAME).log(level, msg, extra=extra)
    return event_dict
