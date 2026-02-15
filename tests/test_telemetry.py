"""Tests for telemetry setup."""

from unittest.mock import patch

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

from gitlab_copilot_agent.telemetry import (
    add_trace_context,
    emit_to_otel_logs,
    get_tracer,
    init_telemetry,
    shutdown_telemetry,
)


def test_init_telemetry_noop_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """When OTEL_EXPORTER_OTLP_ENDPOINT is unset, tracer provider is not configured."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    init_telemetry()
    # Default provider is a ProxyTracerProvider, not our TracerProvider
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)


def test_init_telemetry_configures_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    with (
        patch("gitlab_copilot_agent.telemetry.OTLPSpanExporter"),
        patch("gitlab_copilot_agent.telemetry.OTLPMetricExporter"),
    ):
        init_telemetry()
        assert isinstance(trace.get_tracer_provider(), TracerProvider)
        assert isinstance(metrics.get_meter_provider(), MeterProvider)
        shutdown_telemetry()
    # Reset to default for other tests
    from opentelemetry.metrics._internal import _ProxyMeterProvider

    trace.set_tracer_provider(trace.ProxyTracerProvider())
    metrics.set_meter_provider(_ProxyMeterProvider())


def test_get_tracer_returns_tracer() -> None:
    tracer = get_tracer("test")
    assert tracer is not None


def test_add_trace_context_with_active_span() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("test-span"):
        event_dict: dict[str, object] = {"event": "test"}
        result = add_trace_context(None, "info", event_dict)
        assert "trace_id" in result
        assert "span_id" in result
        assert len(result["trace_id"]) == 32  # 128-bit hex
    provider.shutdown()


def test_add_trace_context_without_span() -> None:
    event_dict: dict[str, object] = {"event": "test"}
    result = add_trace_context(None, "info", event_dict)
    # No active span — trace_id should not be injected (or be all zeros)
    # The default invalid span has trace_id=0, so we check it's not added
    assert "trace_id" not in result or result["trace_id"] == "0" * 32


def test_emit_to_otel_logs_noop_when_unconfigured() -> None:
    """When OTel logging is not configured, emit_to_otel_logs is a passthrough."""
    event_dict: dict[str, object] = {"event": "test", "level": "info"}
    result = emit_to_otel_logs(None, "info", event_dict)
    assert result is event_dict


def test_emit_to_otel_logs_emits_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import gitlab_copilot_agent.telemetry as tel_mod

    monkeypatch.setattr(tel_mod, "_otel_logging_configured", True)
    with patch("gitlab_copilot_agent.telemetry.logging") as mock_logging:
        mock_logger = mock_logging.getLogger.return_value
        mock_logging.INFO = 20
        event_dict: dict[str, object] = {"event": "clone_done", "level": "info", "branch": "main"}
        emit_to_otel_logs(None, "info", event_dict)
        mock_logger.log.assert_called_once()
        args = mock_logger.log.call_args
        assert args[0][1] == "clone_done"
