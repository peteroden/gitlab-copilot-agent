"""OTLP exporter creation and collector connectivity probing."""

from __future__ import annotations

import os
import threading
from typing import Any
from urllib.parse import urlparse

import structlog

from gitlab_copilot_agent.telemetry import _state

_log = structlog.get_logger()


def use_http_protocol() -> bool:
    """Return True if OTLP HTTP/protobuf protocol is configured."""
    return os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc") == "http/protobuf"


def create_exporters() -> tuple[Any, Any, Any]:
    """Create span, metric, and log exporters based on configured protocol."""
    if use_http_protocol():
        return _create_http_exporters()
    return _create_grpc_exporters()


def _create_http_exporters() -> tuple[Any, Any, Any]:
    from opentelemetry.exporter.otlp.proto.http._log_exporter import (  # noqa: PLC0415
        OTLPLogExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )

    return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()


def _create_grpc_exporters() -> tuple[Any, Any, Any]:
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (  # noqa: PLC0415
        OTLPLogExporter,
    )
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # noqa: PLC0415
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )

    return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()


def check_connectivity(endpoint: str, timeout: float = 3.0) -> bool:
    """Quick connectivity check. Uses HTTP or gRPC based on protocol config."""
    try:
        if use_http_protocol():
            import contextlib  # noqa: PLC0415
            import urllib.error  # noqa: PLC0415
            import urllib.request  # noqa: PLC0415

            url = endpoint.rstrip("/") + "/v1/traces"
            req = urllib.request.Request(url, method="POST", data=b"")
            with contextlib.suppress(urllib.error.HTTPError):
                urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
            return True
        import grpc  # pyright: ignore[reportMissingTypeStubs]  # noqa: PLC0415

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


def schedule_probe(endpoint: str, interval: float = 30.0) -> None:
    """Schedule a background connectivity probe after *interval* seconds."""
    _state.probe_timer = threading.Timer(interval, _run_probe, args=[endpoint, interval])
    _state.probe_timer.daemon = True
    _state.probe_timer.start()


def _run_probe(endpoint: str, interval: float) -> None:
    """Execute a single probe; reschedule if still unreachable."""
    _state.probe_timer = None
    if check_connectivity(endpoint):
        _state.collector_reachable = True
        _log.info(
            "otel_collector_connected",
            endpoint=endpoint,
            msg="Telemetry is now being exported to the collector",
        )
    else:
        schedule_probe(endpoint, interval)
