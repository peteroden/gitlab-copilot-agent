"""OpenTelemetry tracing and log export setup."""

from gitlab_copilot_agent.telemetry.logging import (
    add_trace_context,
    configure_logging,
    emit_to_otel_logs,
)
from gitlab_copilot_agent.telemetry.tracing import (
    get_tracer,
    init_telemetry,
    restore_trace_context,
    shutdown_telemetry,
)

__all__ = [
    "add_trace_context",
    "configure_logging",
    "emit_to_otel_logs",
    "get_tracer",
    "init_telemetry",
    "restore_trace_context",
    "shutdown_telemetry",
]
