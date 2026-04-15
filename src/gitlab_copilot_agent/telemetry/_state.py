"""Shared telemetry state — module-level flags used across submodules."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading

    from opentelemetry.sdk.trace.export import SpanExporter

SERVICE_NAME = "gitlab-copilot-agent"
otel_logging_configured = False
initialized = False
probe_timer: threading.Timer | None = None
collector_reachable = False
span_exporter: SpanExporter | None = None
