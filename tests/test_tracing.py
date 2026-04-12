"""Tests for end-to-end tracing: traceparent propagation, restore, and SDK telemetry."""

from __future__ import annotations

import contextlib
import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import context as context_api
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from gitlab_copilot_agent.remote_executor import RemoteTaskExecutor
from gitlab_copilot_agent.task_runner import QueueTaskPayload
from gitlab_copilot_agent.telemetry.tracing import restore_trace_context

# -- Constants --

VALID_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
EXPECTED_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


class _InMemoryExporter(SpanExporter):
    """Minimal in-memory span collector for tests."""

    def __init__(self) -> None:
        self.spans: list[Any] = []

    def export(self, spans: Any) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


@contextmanager
def _tracing_provider():
    """Set up an in-memory TracerProvider for testing span relationships."""
    exporter = _InMemoryExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    old_provider = trace.get_tracer_provider()
    trace.set_tracer_provider(provider)
    try:
        yield provider, exporter
    finally:
        provider.shutdown()
        trace.set_tracer_provider(old_provider)


def _make_stub_pipeline() -> MagicMock:
    """Create a mock pipeline with all required async methods."""
    p = MagicMock()
    for method in ("prepare", "execute", "process", "cleanup", "handle_error"):
        setattr(p, method, AsyncMock())
    type(p).__name__ = "TestPipeline"
    return p


def _capture_copilot_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    otel_endpoint: str | None,
    copilot_http_endpoint: str | None = None,
) -> list[Any]:
    """Run copilot_session with a fake client and capture the SubprocessConfig."""
    if otel_endpoint:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", otel_endpoint)
    else:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    if copilot_http_endpoint:
        monkeypatch.setenv("COPILOT_OTEL_HTTP_ENDPOINT", copilot_http_endpoint)
    else:
        monkeypatch.delenv("COPILOT_OTEL_HTTP_ENDPOINT", raising=False)

    captured: list[Any] = []

    class _FakeClient:
        def __init__(self, config: Any) -> None:
            captured.append(config)

        async def start(self) -> None:
            pass

        async def get_auth_status(self) -> MagicMock:
            m = MagicMock()
            m.authType = "token"
            m.isAuthenticated = True
            return m

        async def create_session(self, **kw: Any) -> MagicMock:
            s = MagicMock()
            s.send = AsyncMock()
            s.on = MagicMock()
            s.disconnect = AsyncMock()
            return s

        async def stop(self) -> None:
            pass

    return captured, _FakeClient  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# restore_trace_context
# ---------------------------------------------------------------------------


class TestRestoreTraceContext:
    """W3C traceparent extraction and round-trip verification."""

    @pytest.mark.parametrize(
        ("traceparent", "tracestate", "expected_none"),
        [
            ("", "", True),
            (VALID_TRACEPARENT, "", False),
            (VALID_TRACEPARENT, "congo=t61rcWkgMzE", False),
            ("not-a-valid-traceparent", "", False),
        ],
        ids=["empty", "valid", "with-tracestate", "invalid-no-crash"],
    )
    def test_restore_returns_context_or_none(
        self, traceparent: str, tracestate: str, expected_none: bool
    ) -> None:
        ctx = restore_trace_context(traceparent, tracestate)
        assert (ctx is None) == expected_none

    def test_restored_trace_id_matches(self) -> None:
        ctx = restore_trace_context(VALID_TRACEPARENT)
        assert ctx is not None
        span_ctx = trace.get_current_span(ctx).get_span_context()
        assert format(span_ctx.trace_id, "032x") == EXPECTED_TRACE_ID

    def test_roundtrip_parent_child_linkage(self) -> None:
        """Serialize → restore → child span has correct parent."""
        with _tracing_provider() as (provider, _):
            tracer = provider.get_tracer("test")
            with tracer.start_as_current_span("parent") as parent_span:
                carrier: dict[str, str] = {}
                propagate.inject(carrier, context=context_api.get_current())
                parent_id = parent_span.get_span_context().span_id

            restored = restore_trace_context(carrier["traceparent"])
            assert restored is not None
            token = context_api.attach(restored)
            try:
                with tracer.start_as_current_span("child") as child:
                    child_parent = child.parent.span_id if child.parent else 0  # type: ignore[union-attr]
            finally:
                context_api.detach(token)
            assert child_parent == parent_id


# ---------------------------------------------------------------------------
# Queue payload traceparent
# ---------------------------------------------------------------------------


class TestQueuePayloadTraceparent:
    """Traceparent fields on QueueTaskPayload and RemoteTaskExecutor payloads."""

    @pytest.mark.parametrize(
        ("extra_fields", "expected_tp", "expected_ts"),
        [
            ({}, "", ""),
            ({"traceparent": "00-abc-def-01", "tracestate": "v=1"}, "00-abc-def-01", "v=1"),
        ],
        ids=["defaults-empty", "round-trips-from-json"],
    )
    def test_payload_traceparent_field(
        self, extra_fields: dict[str, str], expected_tp: str, expected_ts: str
    ) -> None:
        base = {"task_type": "review", "task_id": "t1", "user_prompt": "test"}
        payload = QueueTaskPayload.model_validate_json(json.dumps(base | extra_fields))
        assert payload.traceparent == expected_tp
        assert payload.tracestate == expected_ts

    async def test_executor_injects_traceparent(self) -> None:
        """RemoteTaskExecutor includes traceparent in queue dispatch payload."""
        from gitlab_copilot_agent.concurrency import MemoryResultStore, MemoryTaskQueue
        from gitlab_copilot_agent.task_executor import TaskParams
        from tests.conftest import make_settings

        queue = MemoryTaskQueue()
        executor = RemoteTaskExecutor(MemoryResultStore(), queue, job_timeout=1)
        task = TaskParams(
            task_type="review",
            task_id="trace-test",
            repo_url="https://gitlab.example.com/g/p.git",
            branch="main",
            system_prompt="sys",
            user_prompt="usr",
            settings=make_settings(),
        )
        with (
            _tracing_provider() as (provider, _),
            patch(
                "gitlab_copilot_agent.remote_executor.tar_repo_to_bytes",
                new=AsyncMock(return_value=b"tar"),
            ),
        ):
            tracer = provider.get_tracer("test")
            with tracer.start_as_current_span("controller"), pytest.raises(TimeoutError):
                await executor.execute(task)

        msg = json.loads(queue._messages[0].payload)
        assert msg["traceparent"].startswith("00-")
        assert "tracestate" in msg


# ---------------------------------------------------------------------------
# Copilot SDK TelemetryConfig
# ---------------------------------------------------------------------------


class TestCopilotTelemetryConfig:
    """TelemetryConfig is passed to SubprocessConfig based on OTEL env vars."""

    @pytest.mark.parametrize(
        ("otel_ep", "http_ep", "expected_otlp"),
        [
            ("http://collector:4317", None, "http://collector:4318"),
            (None, None, None),
            ("http://host:4317", "http://custom:9999", "http://custom:9999"),
        ],
        ids=["derives-http-from-grpc", "disabled-when-unset", "explicit-http-override"],
    )
    async def test_telemetry_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        otel_ep: str | None,
        http_ep: str | None,
        expected_otlp: str | None,
    ) -> None:
        captured, fake_cls = _capture_copilot_config(
            monkeypatch, otel_endpoint=otel_ep, copilot_http_endpoint=http_ep
        )
        with (
            patch("gitlab_copilot_agent.copilot_session.CopilotClient", fake_cls),
            patch("gitlab_copilot_agent.copilot_session.get_real_cli_path", return_value="/x"),
            patch(
                "gitlab_copilot_agent.copilot_session.discover_repo_config",
                return_value=MagicMock(
                    instructions=None, skill_directories=None, custom_agents=None
                ),
            ),
        ):
            from gitlab_copilot_agent.copilot_session import run_copilot_session
            from tests.conftest import make_settings

            with contextlib.suppress(Exception):
                await run_copilot_session(make_settings(), "/tmp/r", "s", "u", timeout=1)

        assert len(captured) == 1
        tel = captured[0].telemetry
        if expected_otlp is None:
            assert tel is None
        else:
            assert tel["otlp_endpoint"] == expected_otlp


# ---------------------------------------------------------------------------
# Pipeline span_attributes
# ---------------------------------------------------------------------------


class TestPipelineSpanAttributes:
    """run_pipeline() forwards span_attributes to the parent span."""

    async def test_span_attributes_applied(self) -> None:
        from gitlab_copilot_agent.pipeline import BasePipelineContext, run_pipeline

        attrs = {"project_id": 42, "mr_iid": 7, "task_type": "review"}
        exporter = _InMemoryExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        with patch("gitlab_copilot_agent.pipeline._tracer", provider.get_tracer("pipeline")):
            await run_pipeline(_make_stub_pipeline(), BasePipelineContext(), span_attributes=attrs)  # type: ignore[arg-type]

        parent = [s for s in exporter.spans if s.name == "pipeline.TestPipeline"]
        assert len(parent) == 1
        assert dict(parent[0].attributes or {}) == attrs
        provider.shutdown()

    async def test_no_span_attributes_default(self) -> None:
        """Backward compat: works without span_attributes."""
        from gitlab_copilot_agent.pipeline import BasePipelineContext, run_pipeline

        result = await run_pipeline(_make_stub_pipeline(), BasePipelineContext())  # type: ignore[arg-type]
        assert result.outcome == "success"
