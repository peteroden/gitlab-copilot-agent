"""Structlog configuration and log processors."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog
from opentelemetry import trace


def configure_logging() -> None:
    """Set up all logging: structlog processors and stdlib routing.

    Call once at module load before any log output.
    Reads LOG_LEVEL env var (default: INFO).
    """
    # Suppress gRPC C-core abseil noise (init warnings before absl::InitializeLog)
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # Plain output in containers (no ANSI color codes); colors only in local TTY
    renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    # Structlog pipeline
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            add_trace_context,  # pyright: ignore[reportArgumentType]
            emit_to_otel_logs,  # pyright: ignore[reportArgumentType]
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    # Route stdlib logging (uvicorn, OTEL SDK, Copilot SDK, etc.) through structlog.
    # add_trace_context injects trace_id/span_id into every foreign log record
    # so Copilot SDK and httpx logs carry the active span's context.
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            add_trace_context,  # pyright: ignore[reportArgumentType]
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Suppress noisy libraries — keep at WARNING+ even when root is DEBUG
    for name in (
        "opentelemetry.exporter.otlp.proto.grpc",
        "opentelemetry.sdk.trace.export",
        "opentelemetry.sdk.metrics.export",
        "opentelemetry.sdk._logs.export",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)
    suppress_noisy_loggers()


def suppress_noisy_loggers() -> None:
    """Suppress chatty HTTP/auth/access loggers at WARNING+.

    Called from both configure_logging() and init_telemetry() because
    OTEL instrumentation can re-enable debug logging on httpcore/httpx.
    """
    for name in ("httpcore", "httpx", "azure", "msal"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Suppress uvicorn access logs (health-check probe spam)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


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
    from gitlab_copilot_agent.telemetry import _state  # noqa: PLC0415

    if not _state.otel_logging_configured:
        return event_dict

    level_name = event_dict.get("level", "info").upper()
    level = getattr(logging, level_name, logging.INFO)
    msg = event_dict.get("event", "")
    # Filter out keys that conflict with stdlib LogRecord reserved attributes.
    # "message" is set by LogRecord.getMessage(); the rest are constructor args.
    _reserved = {
        "event",
        "level",
        "timestamp",
        "exc_info",
        "stack_info",
        "stackLevel",
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_text",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    }
    extra = {k: v for k, v in event_dict.items() if k not in _reserved}
    logging.getLogger(_state.SERVICE_NAME).log(level, msg, extra=extra)

    return event_dict
