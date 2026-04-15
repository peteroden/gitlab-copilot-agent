"""OpenTelemetry tracing initialization, shutdown, and tracer access."""

from __future__ import annotations

import logging
import os

import structlog
from opentelemetry import context as context_api
from opentelemetry import metrics, propagate, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from gitlab_copilot_agent.telemetry import _state
from gitlab_copilot_agent.telemetry.exporters import (
    check_connectivity,
    create_exporters,
    schedule_probe,
)
from gitlab_copilot_agent.telemetry.logging import suppress_noisy_loggers

_log = structlog.get_logger()


def init_telemetry() -> None:
    """Configure OpenTelemetry tracing, metrics, and log export.

    No-op if OTEL_EXPORTER_OTLP_ENDPOINT is unset or already initialized.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint or _state.initialized:
        return

    from opentelemetry._logs import set_logger_provider  # noqa: PLC0415
    from opentelemetry.instrumentation.httpx import (  # noqa: PLC0415
        HTTPXClientInstrumentor,
    )
    from opentelemetry.sdk._logs import (  # noqa: PLC0415
        LoggerProvider,
        LoggingHandler,
    )
    from opentelemetry.sdk._logs.export import (  # noqa: PLC0415
        BatchLogRecordProcessor,
    )
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", _state.SERVICE_NAME),
            "service.version": os.environ.get("SERVICE_VERSION", "0.1.0"),
            "deployment.environment": os.environ.get("DEPLOYMENT_ENV", ""),
        }
    )

    span_exporter, metric_exporter, log_exporter = create_exporters()
    _state.span_exporter = span_exporter

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
    otel_logger = logging.getLogger(_state.SERVICE_NAME)
    otel_logger.addHandler(handler)
    otel_logger.setLevel(logging.DEBUG)
    otel_logger.propagate = False  # Don't duplicate to root/console
    _state.otel_logging_configured = True

    # Auto-instrument httpx for HTTP client metrics and traces
    HTTPXClientInstrumentor().instrument()

    # Re-suppress libraries that OTEL instrumentation may have re-enabled
    suppress_noisy_loggers()

    _state.initialized = True

    # Connectivity probe — informational only (SDK buffers internally)
    if check_connectivity(endpoint):
        _state.collector_reachable = True
        _log.info("otel_collector_connected", endpoint=endpoint)
    else:
        _log.warning(
            "otel_collector_unavailable",
            endpoint=endpoint,
            msg="Telemetry will be exported once the collector is reachable. "
            "Retrying in background every 30s.",
        )
        schedule_probe(endpoint)


def shutdown_telemetry() -> None:
    """Flush and shutdown providers."""
    if _state.probe_timer is not None:
        _state.probe_timer.cancel()
        _state.probe_timer = None

    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()

    meter_provider = metrics.get_meter_provider()
    if isinstance(meter_provider, MeterProvider):
        meter_provider.shutdown()

    if _state.otel_logging_configured:
        from opentelemetry._logs import get_logger_provider  # noqa: PLC0415
        from opentelemetry.sdk._logs import LoggerProvider  # noqa: PLC0415

        log_provider = get_logger_provider()
        if isinstance(log_provider, LoggerProvider):
            log_provider.shutdown()  # pyright: ignore[reportUnknownMemberType]

    _state.initialized = False
    _state.otel_logging_configured = False
    _state.collector_reachable = False


def get_tracer(name: str) -> trace.Tracer:
    """Get a tracer instance."""
    return trace.get_tracer(name)


def restore_trace_context(traceparent: str, tracestate: str = "") -> context_api.Context | None:
    """Restore W3C trace context from propagated headers.

    Returns the extracted context, or ``None`` if *traceparent* is empty.
    Callers use ``context_api.attach(ctx)`` to activate it.

    Trust boundary note: traceparent is serialized by the controller and
    deserialized by the task runner.  An attacker with queue write access
    could inject arbitrary trace context, but queue access already implies
    full system compromise.  No integrity check is needed.
    """
    if not traceparent:
        return None
    carrier: dict[str, str] = {"traceparent": traceparent}
    if tracestate:
        carrier["tracestate"] = tracestate
    return propagate.extract(carrier)
