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
    """TelemetryConfig uses file exporter when OTEL is configured."""

    @pytest.mark.parametrize(
        ("otel_ep", "expect_file"),
        [
            ("http://collector:4317", True),
            (None, False),
        ],
        ids=["file-exporter-when-otel-set", "disabled-when-unset"],
    )
    async def test_telemetry_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        otel_ep: str | None,
        expect_file: bool,
    ) -> None:
        if otel_ep:
            monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", otel_ep)
        else:
            monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
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

        with (
            patch("gitlab_copilot_agent.copilot_session.CopilotClient", _FakeClient),
            patch("gitlab_copilot_agent.copilot_session.get_real_cli_path", return_value="/x"),
            patch(
                "gitlab_copilot_agent.copilot_session.discover_repo_config",
                return_value=MagicMock(
                    instructions=None, skill_directories=None, custom_agents=None
                ),
            ),
            patch("gitlab_copilot_agent.copilot_session.forward_cli_traces"),
        ):
            from gitlab_copilot_agent.copilot_session import run_copilot_session
            from tests.conftest import make_settings

            with contextlib.suppress(Exception):
                await run_copilot_session(make_settings(), "/tmp/r", "s", "u", timeout=1)

        assert len(captured) == 1
        tel = captured[0].telemetry
        if expect_file:
            assert tel is not None
            assert "file_path" in tel
            assert tel["file_path"].endswith("cli-traces.jsonl")
        else:
            assert tel is None


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


# ---------------------------------------------------------------------------
# CLI trace forwarder
# ---------------------------------------------------------------------------

CLI_SPAN_JSONL = json.dumps(
    {
        "type": "span",
        "traceId": EXPECTED_TRACE_ID,
        "spanId": "b7ad6b7169203331",
        "parentSpanId": "1111111111111111",
        "name": "chat gpt-4.1",
        "kind": 2,
        "startTime": [1700000000, 100000000],
        "endTime": [1700000003, 200000000],
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4.1",
            "gen_ai.response.finish_reasons": ["stop"],
        },
        "status": {"code": 0},
        "resource": {"attributes": {"service.name": "github-copilot"}},
        "instrumentationScope": {"name": "github.copilot", "version": "1.0.17"},
    }
)


class TestCliTraceForwarder:
    """Tests for telemetry.cli_trace_forwarder."""

    def test_forward_parses_and_exports(self, tmp_path: Any) -> None:
        trace_file = tmp_path / "traces.jsonl"
        trace_file.write_text(CLI_SPAN_JSONL + "\n")

        from unittest.mock import MagicMock

        from gitlab_copilot_agent.telemetry import _state
        from gitlab_copilot_agent.telemetry.cli_trace_forwarder import forward_cli_traces

        mock_exporter = MagicMock()
        old = _state.span_exporter
        _state.span_exporter = mock_exporter
        try:
            count = forward_cli_traces(str(trace_file))
        finally:
            _state.span_exporter = old

        assert count == 1
        mock_exporter.export.assert_called_once()
        spans = mock_exporter.export.call_args[0][0]
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "chat gpt-4.1"
        assert format(span.context.trace_id, "032x") == EXPECTED_TRACE_ID
        assert format(span.context.span_id, "016x") == "b7ad6b7169203331"
        assert format(span.parent.span_id, "016x") == "1111111111111111"
        # Verify real timestamps are preserved, not parse time
        assert span.start_time == 1700000000 * 10**9 + 100000000
        assert span.end_time == 1700000003 * 10**9 + 200000000

    def test_forward_skips_when_no_exporter(self, tmp_path: Any) -> None:
        trace_file = tmp_path / "traces.jsonl"
        trace_file.write_text(CLI_SPAN_JSONL + "\n")

        from gitlab_copilot_agent.telemetry import _state
        from gitlab_copilot_agent.telemetry.cli_trace_forwarder import forward_cli_traces

        old = _state.span_exporter
        _state.span_exporter = None
        try:
            assert forward_cli_traces(str(trace_file)) == 0
        finally:
            _state.span_exporter = old

    def test_forward_skips_missing_file(self) -> None:
        from gitlab_copilot_agent.telemetry.cli_trace_forwarder import forward_cli_traces

        assert forward_cli_traces("/nonexistent/path.jsonl") == 0

    def test_forward_tolerates_malformed_lines(self, tmp_path: Any) -> None:
        trace_file = tmp_path / "traces.jsonl"
        trace_file.write_text("not json\n" + CLI_SPAN_JSONL + "\n")

        from unittest.mock import MagicMock

        from gitlab_copilot_agent.telemetry import _state
        from gitlab_copilot_agent.telemetry.cli_trace_forwarder import forward_cli_traces

        mock_exporter = MagicMock()
        old = _state.span_exporter
        _state.span_exporter = mock_exporter
        try:
            count = forward_cli_traces(str(trace_file))
        finally:
            _state.span_exporter = old

        assert count == 1  # malformed line skipped, valid span exported
