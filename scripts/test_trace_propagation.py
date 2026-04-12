#!/usr/bin/env python3
"""E2E smoke test: verify trace context propagates from app → Copilot SDK → CLI.

Prerequisite: start the console collector in another terminal:
    uv run python scripts/otel_console_collector.py --verbose

Then run this script:
    GITHUB_TOKEN=$(gh auth token) uv run python scripts/test_trace_propagation.py

The --verbose collector output shows per-span trace IDs and parent linkage.
All CLI spans (invoke_agent, chat) should share the app's trace ID, and
invoke_agent should have parent = the app's span ID.

Alternatively, use file_path in TelemetryConfig for offline verification
(but note: the CLI only supports one exporter at a time).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile


async def _run() -> None:
    collector = os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
    )
    http_endpoint = os.environ.get(
        "COPILOT_OTEL_HTTP_ENDPOINT", "http://localhost:4318"
    )

    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        print("✗ GITHUB_TOKEN not set. Try: GITHUB_TOKEN=$(gh auth token) uv run ...")
        sys.exit(1)

    # Initialize OTEL tracing in this process so spans export to the collector
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = collector
    os.environ.setdefault("OTEL_SERVICE_NAME", "trace-propagation-test")

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError:
        print("✗ opentelemetry-exporter-otlp-proto-grpc not installed")
        sys.exit(1)

    resource = Resource.create({"service.name": "trace-propagation-test"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=collector)))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test-harness")

    print(f"  OTLP gRPC endpoint: {collector}")
    print(f"  CLI HTTP endpoint:  {http_endpoint}")
    print()

    from copilot import CopilotClient
    from copilot.client import SubprocessConfig, TelemetryConfig

    session_home = tempfile.mkdtemp(prefix="copilot-trace-test-")

    with tracer.start_as_current_span("test.trace_propagation") as span:
        ctx = span.get_span_context()
        app_trace_id = format(ctx.trace_id, "032x")
        app_span_id = format(ctx.span_id, "016x")
        print(f"  App trace ID: {app_trace_id}")
        print(f"  App span ID:  {app_span_id}")
        print()

        client = CopilotClient(SubprocessConfig(
            github_token=github_token,
            env={**os.environ, "HOME": session_home},
            telemetry=TelemetryConfig(otlp_endpoint=http_endpoint),
        ))
        await client.start()

        try:
            from copilot.session import PermissionHandler

            session = await client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                working_directory="/tmp",
                model="gpt-4.1",
                tools=[],
            )

            result = await session.send_and_wait("Say OK", timeout=30)
            response = ""
            if result and hasattr(result.data, "content"):
                response = result.data.content or ""

            print(f"  SDK response: {response.strip()[:80]}")

            # The CLI's OTLP batch exporter fires on a schedule (~5s).
            # Keep the session alive so spans export to the collector.
            print("  Waiting for CLI OTLP batch export...")
            await asyncio.sleep(12)

            await session.disconnect()
        finally:
            await client.stop()

    # Flush app spans to the collector
    provider.force_flush(timeout_millis=5000)
    provider.shutdown()

    print()
    print("✓ Done. Check the --verbose collector output.")
    print(f"  Expected: all CLI spans have trace={app_trace_id}")
    print(f"  Expected: invoke_agent has parent={app_span_id}")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
