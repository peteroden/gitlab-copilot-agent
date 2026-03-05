"""OpenTelemetry tracing and log export setup."""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any
from urllib.parse import urlparse

import structlog
from opentelemetry import metrics, trace
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


def configure_logging() -> None:
    """Set up all logging: structlog processors and stdlib routing.

    Call once at module load before any log output.
    """
    # Suppress gRPC C-core abseil noise (init warnings before absl::InitializeLog)
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

    renderer = structlog.dev.ConsoleRenderer()

    # Structlog pipeline
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            add_trace_context,  # type: ignore[list-item]
            emit_to_otel_logs,  # type: ignore[list-item]
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    # Route stdlib logging (uvicorn, OTEL SDK, etc.) through structlog
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
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


def _check_connectivity(endpoint: str, timeout: float = 3.0) -> bool:
    """Quick connectivity check. Uses HTTP or gRPC based on protocol config."""
    try:
        if _use_http_protocol():
            import contextlib  # noqa: PLC0415
            import urllib.error  # noqa: PLC0415
            import urllib.request  # noqa: PLC0415

            url = endpoint.rstrip("/") + "/v1/traces"
            req = urllib.request.Request(url, method="POST", data=b"")
            with contextlib.suppress(urllib.error.HTTPError):
                urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
            return True
        else:
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
    if _check_connectivity(endpoint):
        _collector_reachable = True
        _log.info(
            "otel_collector_connected",
            endpoint=endpoint,
            msg="Telemetry is now being exported to the collector",
        )
    else:
        _schedule_probe(endpoint, interval)


def _use_http_protocol() -> bool:
    """Return True if OTLP HTTP/protobuf protocol is configured."""
    return os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc") == "http/protobuf"


def _create_exporters() -> tuple[Any, Any, Any]:
    """Create span, metric, and log exporters based on configured protocol."""
    if _use_http_protocol():
        return _create_http_exporters()
    return _create_grpc_exporters()


def _create_http_exporters() -> tuple[Any, Any, Any]:
    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter,  # noqa: PLC0415
    )
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,  # noqa: PLC0415
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,  # noqa: PLC0415
    )

    return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()


def _create_grpc_exporters() -> tuple[Any, Any, Any]:
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
        OTLPLogExporter,  # noqa: PLC0415
    )
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,  # noqa: PLC0415
    )
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,  # noqa: PLC0415
    )

    return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()


def init_telemetry() -> None:
    """Configure OpenTelemetry tracing, metrics, and log export.

    No-op if OTEL_EXPORTER_OTLP_ENDPOINT is unset or already initialized.
    """
    global _otel_logging_configured, _initialized, _collector_reachable  # noqa: PLW0603
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint or _initialized:
        return

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", _SERVICE_NAME),
            "service.version": os.environ.get("SERVICE_VERSION", "0.1.0"),
            "deployment.environment": os.environ.get("DEPLOYMENT_ENV", ""),
        }
    )

    span_exporter, metric_exporter, log_exporter = _create_exporters()

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Metrics
    metric_reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(metric_readers=[metric_reader], resource=resource)
    metrics.set_meter_provider(meter_provider)

    # Logs → OTLP
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
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
    if _check_connectivity(endpoint):
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
