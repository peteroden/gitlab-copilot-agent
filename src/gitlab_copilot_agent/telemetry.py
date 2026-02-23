"""OpenTelemetry tracing and log export setup."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any
from urllib.parse import urlparse

import structlog
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = "gitlab-copilot-agent"
_otel_logging_configured = False
_initialized = False
_probe_timer: threading.Timer | None = None
_collector_reachable = False

_log = structlog.get_logger()


def configure_stdlib_logging() -> None:
    """Route stdlib logging through structlog so all output is consistent.

    This makes OTEL SDK messages (and any other stdlib logger) flow through
    structlog's processor chain, producing timestamped key=value output.
    """
    # Suppress gRPC C-core abseil noise (init warnings before absl::InitializeLog)
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(),
        foreign_pre_chain=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
        ],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Suppress OTEL SDK exporter retry noise (transient gRPC errors logged at WARNING)
    for name in (
        "opentelemetry.exporter.otlp.proto.grpc",
        "opentelemetry.sdk.trace.export",
        "opentelemetry.sdk.metrics.export",
        "opentelemetry.sdk._logs.export",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)


def _check_grpc_connectivity(endpoint: str, timeout: float = 3.0) -> bool:
    """Quick gRPC connectivity check. Returns True if the endpoint is reachable."""
    try:
        import grpc  # type: ignore[import-untyped]  # noqa: PLC0415

        parsed = urlparse(endpoint)
        target = f"{parsed.hostname}:{parsed.port or 4317}"
        if parsed.scheme == "https":
            channel = grpc.secure_channel(target, grpc.ssl_channel_credentials())
        else:
            channel = grpc.insecure_channel(target)
        try:
            grpc.channel_ready_future(channel).result(timeout=timeout)
            return True
        except grpc.FutureTimeoutError:
            return False
        finally:
            channel.close()
    except Exception:
        return False


def _schedule_probe(endpoint: str, interval: float = 30.0) -> None:
    """Schedule a background connectivity probe after *interval* seconds."""
    global _probe_timer  # noqa: PLW0603
    _probe_timer = threading.Timer(interval, _run_probe, args=[endpoint, interval])
    _probe_timer.daemon = True
    _probe_timer.start()


def _run_probe(endpoint: str, interval: float) -> None:
    """Execute a single probe; reschedule if still unreachable."""
    global _collector_reachable, _probe_timer  # noqa: PLW0603
    _probe_timer = None
    if _check_grpc_connectivity(endpoint):
        _collector_reachable = True
        _log.info(
            "otel_collector_connected",
            endpoint=endpoint,
            msg="Telemetry is now being exported to the collector",
        )
    else:
        _schedule_probe(endpoint, interval)


def init_telemetry() -> None:
    """Configure OpenTelemetry tracing, metrics, and log export.

    No-op if OTEL_EXPORTER_OTLP_ENDPOINT is unset or already initialized.
    """
    global _otel_logging_configured, _initialized, _collector_reachable  # noqa: PLW0603
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint or _initialized:
        return

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create(
        {
            "service.name": _SERVICE_NAME,
            "service.version": os.environ.get("SERVICE_VERSION", "0.1.0"),
            "deployment.environment": os.environ.get("DEPLOYMENT_ENV", ""),
        }
    )

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    # Metrics
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(metric_readers=[metric_reader], resource=resource)
    metrics.set_meter_provider(meter_provider)

    # Logs → OTLP
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)

    handler = LoggingHandler(logger_provider=logger_provider)
    otel_logger = logging.getLogger(_SERVICE_NAME)
    otel_logger.addHandler(handler)
    otel_logger.setLevel(logging.DEBUG)
    otel_logger.propagate = False  # Don't duplicate to root/console
    _otel_logging_configured = True

    # Auto-instrument httpx for HTTP client metrics and traces
    HTTPXClientInstrumentor().instrument()

    _initialized = True

    # Connectivity probe — informational only (SDK buffers internally)
    if _check_grpc_connectivity(endpoint):
        _collector_reachable = True
        _log.info("otel_collector_connected", endpoint=endpoint)
    else:
        _log.warning(
            "otel_collector_unavailable",
            endpoint=endpoint,
            msg="Telemetry will be exported once the collector is reachable. "
            "Retrying in background every 30s.",
        )
        _schedule_probe(endpoint)


def shutdown_telemetry() -> None:
    """Flush and shutdown providers."""
    global _initialized, _otel_logging_configured, _probe_timer, _collector_reachable  # noqa: PLW0603
    if _probe_timer is not None:
        _probe_timer.cancel()
        _probe_timer = None

    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()

    meter_provider = metrics.get_meter_provider()
    if isinstance(meter_provider, MeterProvider):
        meter_provider.shutdown()

    if _otel_logging_configured:
        from opentelemetry._logs import get_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider

        log_provider = get_logger_provider()
        if isinstance(log_provider, LoggerProvider):
            log_provider.shutdown()  # type: ignore[no-untyped-call]

    _initialized = False
    _otel_logging_configured = False
    _collector_reachable = False


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
    logging.getLogger(_SERVICE_NAME).log(level, msg, extra=extra)
    return event_dict
