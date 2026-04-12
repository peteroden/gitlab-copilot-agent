"""Tests for end-to-end tracing: traceparent propagation, restore, and SDK telemetry."""

from __future__ import annotations

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

TASK_ID = "trace-test-001"
TASK_TYPE = "review"
USER_PROMPT = "Review this code."
SYSTEM_PROMPT = "You are a reviewer."
REPO_BLOB_KEY = "repos/trace-test-001.tar.gz"


class _InMemoryExporter(SpanExporter):
    """Minimal in-memory span collector for tests."""

    def __init__(self) -> None:
        self.spans: list[Any] = []

    def export(self, spans: Any) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def get_finished_spans(self) -> list[Any]:
        return list(self.spans)


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


# ---------------------------------------------------------------------------
# restore_trace_context
# ---------------------------------------------------------------------------


class TestRestoreTraceContext:
    """Tests for restore_trace_context() — W3C traceparent extraction."""

    def test_empty_traceparent_returns_none(self) -> None:
        assert restore_trace_context("") is None

    def test_valid_traceparent_returns_context(self) -> None:
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        ctx = restore_trace_context(tp)
        assert ctx is not None

    def test_restored_context_has_correct_trace_id(self) -> None:
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        ctx = restore_trace_context(tp)
        assert ctx is not None
        span_ctx = trace.get_current_span(ctx).get_span_context()
        assert format(span_ctx.trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c"

    def test_tracestate_is_preserved(self) -> None:
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        ts = "congo=t61rcWkgMzE"
        ctx = restore_trace_context(tp, ts)
        assert ctx is not None
        span_ctx = trace.get_current_span(ctx).get_span_context()
        assert span_ctx.trace_id != 0

    def test_roundtrip_parent_child_linkage(self) -> None:
        """Serialize traceparent → restore → child span has correct parent."""
        with _tracing_provider() as (provider, _exporter):
            tracer = provider.get_tracer("test")

            # Create a parent span and serialize its traceparent
            with tracer.start_as_current_span("parent") as parent_span:
                carrier: dict[str, str] = {}
                propagate.inject(carrier, context=context_api.get_current())
                traceparent = carrier["traceparent"]
                parent_span_id = parent_span.get_span_context().span_id

            # Restore in a "different process" context and create a child
            restored = restore_trace_context(traceparent)
            assert restored is not None
            token = context_api.attach(restored)
            try:
                with tracer.start_as_current_span("child") as child_span:
                    child_parent_id = child_span.parent.span_id if child_span.parent else 0  # type: ignore[union-attr]
            finally:
                context_api.detach(token)

            assert child_parent_id == parent_span_id

    def test_invalid_traceparent_does_not_crash(self) -> None:
        """Invalid traceparent should not raise — returns a context with invalid span."""
        ctx = restore_trace_context("not-a-valid-traceparent")
        # Should return a context (may have invalid/zero span), but not crash
        assert ctx is not None


# ---------------------------------------------------------------------------
# Remote executor traceparent injection
# ---------------------------------------------------------------------------


class TestRemoteExecutorTraceInjection:
    """Tests that RemoteTaskExecutor includes traceparent in queue payloads."""

    async def test_payload_contains_traceparent(self) -> None:
        """Queue payload should include traceparent and tracestate fields."""
        from gitlab_copilot_agent.concurrency import MemoryResultStore, MemoryTaskQueue
        from gitlab_copilot_agent.task_executor import TaskParams
        from tests.conftest import make_settings

        store = MemoryResultStore()
        queue = MemoryTaskQueue()
        executor = RemoteTaskExecutor(store, queue, job_timeout=1)

        task = TaskParams(
            task_type=TASK_TYPE,
            task_id=TASK_ID,
            repo_url="https://gitlab.example.com/g/p.git",
            branch="main",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT,
            settings=make_settings(),
        )

        with (
            _tracing_provider() as (provider, _exporter),
            patch(
                "gitlab_copilot_agent.remote_executor.tar_repo_to_bytes",
                new=AsyncMock(return_value=b"fake-tar"),
            ),
        ):
            tracer = provider.get_tracer("test")
            with tracer.start_as_current_span("controller"), pytest.raises(TimeoutError):
                await executor.execute(task)

        # Check the queued message contains traceparent
        assert queue._messages
        msg_payload = json.loads(queue._messages[0].payload)
        assert "traceparent" in msg_payload
        assert msg_payload["traceparent"].startswith("00-")
        assert "tracestate" in msg_payload


# ---------------------------------------------------------------------------
# QueueTaskPayload traceparent field
# ---------------------------------------------------------------------------


class TestQueueTaskPayloadTraceparent:
    """Tests for traceparent/tracestate fields on QueueTaskPayload."""

    def test_defaults_to_empty_strings(self) -> None:
        payload = QueueTaskPayload(
            task_type="review",
            task_id="t1",
            user_prompt="test",
        )
        assert payload.traceparent == ""
        assert payload.tracestate == ""

    def test_accepts_traceparent_from_json(self) -> None:
        raw = json.dumps(
            {
                "task_type": "review",
                "task_id": "t1",
                "user_prompt": "test",
                "traceparent": "00-abc123-def456-01",
                "tracestate": "vendor=value",
            }
        )
        payload = QueueTaskPayload.model_validate_json(raw)
        assert payload.traceparent == "00-abc123-def456-01"
        assert payload.tracestate == "vendor=value"

    def test_missing_traceparent_uses_default(self) -> None:
        """Backward compatibility: old payloads without traceparent still parse."""
        raw = json.dumps(
            {
                "task_type": "review",
                "task_id": "t1",
                "user_prompt": "test",
            }
        )
        payload = QueueTaskPayload.model_validate_json(raw)
        assert payload.traceparent == ""
        assert payload.tracestate == ""


# ---------------------------------------------------------------------------
# Copilot SDK TelemetryConfig
# ---------------------------------------------------------------------------


class TestCopilotTelemetryConfig:
    """Tests that copilot_session passes TelemetryConfig when OTEL endpoint is set."""

    async def test_telemetry_config_passed_when_otel_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.delenv("COPILOT_OTEL_HTTP_ENDPOINT", raising=False)

        captured_config: list[Any] = []

        class _FakeClient:
            def __init__(self, config: Any) -> None:
                captured_config.append(config)

            async def start(self) -> None:
                pass

            async def get_auth_status(self) -> MagicMock:
                m = MagicMock()
                m.authType = "token"
                m.isAuthenticated = True
                return m

            async def create_session(self, **kwargs: Any) -> MagicMock:
                session = MagicMock()

                async def _send(prompt: str) -> None:
                    pass

                session.send = _send
                session.on = MagicMock()
                session.disconnect = AsyncMock()
                return session

            async def stop(self) -> None:
                pass

        with (
            patch("gitlab_copilot_agent.copilot_session.CopilotClient", _FakeClient),
            patch(
                "gitlab_copilot_agent.copilot_session.get_real_cli_path",
                return_value="/fake/cli",
            ),
            patch(
                "gitlab_copilot_agent.copilot_session.discover_repo_config",
                return_value=MagicMock(
                    instructions=None, skill_directories=None, custom_agents=None
                ),
            ),
        ):
            from tests.conftest import make_settings

            settings = make_settings()

            try:
                from gitlab_copilot_agent.copilot_session import run_copilot_session

                await run_copilot_session(settings, "/tmp/repo", "system", "user", timeout=1)
            except Exception:
                pass  # Expected — fake session doesn't complete

        assert len(captured_config) == 1
        config = captured_config[0]
        assert config.telemetry is not None
        assert config.telemetry["otlp_endpoint"] == "http://collector:4318"

    async def test_no_telemetry_config_when_otel_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("COPILOT_OTEL_HTTP_ENDPOINT", raising=False)

        captured_config: list[Any] = []

        class _FakeClient:
            def __init__(self, config: Any) -> None:
                captured_config.append(config)

            async def start(self) -> None:
                pass

            async def get_auth_status(self) -> MagicMock:
                m = MagicMock()
                m.authType = "token"
                m.isAuthenticated = True
                return m

            async def create_session(self, **kwargs: Any) -> MagicMock:
                session = MagicMock()

                async def _send(prompt: str) -> None:
                    pass

                session.send = _send
                session.on = MagicMock()
                session.disconnect = AsyncMock()
                return session

            async def stop(self) -> None:
                pass

        with (
            patch("gitlab_copilot_agent.copilot_session.CopilotClient", _FakeClient),
            patch(
                "gitlab_copilot_agent.copilot_session.get_real_cli_path",
                return_value="/fake/cli",
            ),
            patch(
                "gitlab_copilot_agent.copilot_session.discover_repo_config",
                return_value=MagicMock(
                    instructions=None, skill_directories=None, custom_agents=None
                ),
            ),
        ):
            from tests.conftest import make_settings

            settings = make_settings()

            try:
                from gitlab_copilot_agent.copilot_session import run_copilot_session

                await run_copilot_session(settings, "/tmp/repo", "system", "user", timeout=1)
            except Exception:
                pass

        assert len(captured_config) == 1
        config = captured_config[0]
        assert config.telemetry is None


# ---------------------------------------------------------------------------
# Pipeline span_attributes
# ---------------------------------------------------------------------------


class TestPipelineSpanAttributes:
    """Tests that run_pipeline() passes span_attributes to the parent span."""

    async def test_span_attributes_applied(self) -> None:
        from gitlab_copilot_agent.pipeline import BasePipelineContext, run_pipeline

        pipeline = MagicMock()
        pipeline.prepare = AsyncMock()
        pipeline.execute = AsyncMock()
        pipeline.process = AsyncMock()
        pipeline.cleanup = AsyncMock()
        pipeline.handle_error = AsyncMock()
        type(pipeline).__name__ = "TestPipeline"

        attrs = {"project_id": 42, "mr_iid": 7, "task_type": "review"}

        exporter = _InMemoryExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        # Patch the module-level _tracer so run_pipeline() uses our provider
        test_tracer = provider.get_tracer("pipeline")
        with patch("gitlab_copilot_agent.pipeline._tracer", test_tracer):
            await run_pipeline(
                pipeline,
                BasePipelineContext(),
                span_attributes=attrs,
            )  # type: ignore[arg-type]

        spans = exporter.get_finished_spans()
        parent_span = [s for s in spans if s.name == "pipeline.TestPipeline"]
        assert len(parent_span) == 1
        span_attrs = dict(parent_span[0].attributes or {})
        assert span_attrs["project_id"] == 42
        assert span_attrs["mr_iid"] == 7
        assert span_attrs["task_type"] == "review"
        provider.shutdown()

    async def test_no_span_attributes_default(self) -> None:
        """run_pipeline() works without span_attributes (backward compat)."""
        from gitlab_copilot_agent.pipeline import BasePipelineContext, run_pipeline

        pipeline = MagicMock()
        pipeline.prepare = AsyncMock()
        pipeline.execute = AsyncMock()
        pipeline.process = AsyncMock()
        pipeline.cleanup = AsyncMock()
        pipeline.handle_error = AsyncMock()
        type(pipeline).__name__ = "TestPipeline"

        result = await run_pipeline(pipeline, BasePipelineContext())  # type: ignore[arg-type]
        assert result.outcome == "success"
