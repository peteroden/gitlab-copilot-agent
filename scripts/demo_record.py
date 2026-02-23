#!/usr/bin/env python3
"""Record a 2-3 minute demo video using Playwright and an embedded OTEL collector.

Connects to an existing Chrome session (preserving GitLab + Jira logins),
records three scenes, and saves video to demo-video/.

Prerequisites:
    1. Chrome launched with: --remote-debugging-port=9222
    2. Logged into GitLab and Jira in that browser
    3. Agent running with OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:<otel-port>
    4. Demo environment provisioned (scripts/demo_provision.py)

Usage:
    uv run python scripts/demo_record.py \
        --gitlab-mr-url https://gitlab.com/myorg/copilot-demo/-/merge_requests/1 \
        --jira-board-url https://myorg.atlassian.net/jira/software/projects/DEMO/boards/42

Set --otel-port to match the agent's OTEL_EXPORTER_OTLP_ENDPOINT port (default: 4317).
"""

from __future__ import annotations

import argparse
import queue
import re
import sys
import time
from concurrent import futures
from pathlib import Path

import grpc
import httpx
from opentelemetry.proto.collector.logs.v1 import (
    logs_service_pb2,
    logs_service_pb2_grpc,
)
from opentelemetry.proto.collector.metrics.v1 import (
    metrics_service_pb2,
    metrics_service_pb2_grpc,
)
from opentelemetry.proto.collector.trace.v1 import (
    trace_service_pb2,
    trace_service_pb2_grpc,
)
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

# ---------------------------------------------------------------------------
# Embedded OTEL Collector — receives agent telemetry in-process
# ---------------------------------------------------------------------------

WATCHED_EVENTS = frozenset(
    {
        "review_complete",
        "copilot_command_complete",
        "coding_task_started",
        "coding_task_complete",
    }
)


class _NoOpTrace(trace_service_pb2_grpc.TraceServiceServicer):
    def Export(self, request, context):  # type: ignore[override]
        return trace_service_pb2.ExportTraceServiceResponse()


class _NoOpMetrics(metrics_service_pb2_grpc.MetricsServiceServicer):
    def Export(self, request, context):  # type: ignore[override]
        return metrics_service_pb2.ExportMetricsServiceResponse()


class EventCollector(logs_service_pb2_grpc.LogsServiceServicer):
    """Receives OTLP logs and puts matching events on a queue."""

    def __init__(self) -> None:
        self.events: queue.Queue[str] = queue.Queue()
        self._seen: set[str] = set()

    def Export(self, request, context):  # type: ignore[override]
        for rl in request.resource_logs:
            for sl in rl.scope_logs:
                for lr in sl.log_records:
                    body = lr.body.string_value
                    for event in WATCHED_EVENTS:
                        if event in body:
                            self.events.put(body)
                            self._seen.add(event)
                            _log(f"📡 OTEL event: {event}")
        return logs_service_pb2.ExportLogsServiceResponse()

    def wait_for(self, event: str, timeout: float = 120.0) -> str | None:
        """Block until *event* appears in a log body. Returns the body or None."""
        if event in self._seen:
            return event
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                body = self.events.get(timeout=1.0)
                if event in body:
                    return body
            except queue.Empty:
                continue
        return None

    def has_seen(self, event: str) -> bool:
        """Check if an event has already been received."""
        return event in self._seen


def _start_otel_server(collector: EventCollector, port: int) -> grpc.Server:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(collector, server)
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(_NoOpTrace(), server)
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(_NoOpMetrics(), server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    _log(f"📡 OTEL collector listening on :{port}")
    return server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[demo] {msg}", flush=True)


def _slow_type(page: Page, selector: str, text: str, delay: int = 80) -> None:
    """Type text character-by-character for a natural demo feel."""
    page.click(selector)
    page.type(selector, text, delay=delay)


def _scroll_slowly(page: Page, distance: int = 300, steps: int = 4, pause: float = 1.5) -> None:
    """Scroll down in increments so the viewer can read."""
    for _ in range(steps):
        page.mouse.wheel(0, distance)
        page.wait_for_timeout(int(pause * 1000))


def _wait_and_refresh(
    page: Page,
    collector: EventCollector,
    event: str,
    timeout: float = 120.0,
    fallback_selector: str | None = None,
) -> None:
    """Wait for an OTEL event, then refresh the page.

    Falls back to polling for *fallback_selector* if the event doesn't arrive.
    """
    body = collector.wait_for(event, timeout=timeout)
    if body:
        _log(f"✅ {event} received — refreshing page")
    elif fallback_selector:
        _log(f"⚠️  {event} timeout — falling back to visual check")
        page.wait_for_selector(fallback_selector, timeout=timeout * 1000)
    else:
        _log(f"⚠️  {event} timeout — refreshing anyway")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(1500)


def _jira_transition_to_ai_ready(jira_project_key: str) -> str | None:
    """Find a 'To Do' issue in the Jira project and transition it to 'AI Ready'.

    Uses JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN from environment.
    Returns the issue key or None if no suitable issue found.
    """
    import base64  # noqa: PLC0415
    import os  # noqa: PLC0415

    jira_url = os.environ.get("JIRA_URL", "")
    jira_email = os.environ.get("JIRA_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    if not all([jira_url, jira_email, jira_token]):
        _log("⚠️  JIRA_URL/JIRA_EMAIL/JIRA_API_TOKEN not set — manual transition required")
        return None

    auth = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # Find a To Do issue
    jql = f'project = "{jira_project_key}" AND status = "To Do"'
    resp = httpx.get(
        f"{jira_url.rstrip('/')}/rest/api/3/search/jql",
        params={"jql": jql, "maxResults": "1", "fields": "summary,status"},
        headers=headers,
        timeout=30.0,
    )
    resp.raise_for_status()
    issues = resp.json().get("issues", [])
    if not issues:
        _log("⚠️  No 'To Do' issues found — manual transition required")
        return None

    issue_key = issues[0]["key"]
    _log(f"Found issue {issue_key} — transitioning to AI Ready...")

    # Get available transitions
    resp = httpx.get(
        f"{jira_url.rstrip('/')}/rest/api/3/issue/{issue_key}/transitions",
        headers=headers,
        timeout=30.0,
    )
    resp.raise_for_status()
    transitions = resp.json().get("transitions", [])
    ai_ready = next((t for t in transitions if t["name"].lower() == "ai ready"), None)
    if not ai_ready:
        available = [t["name"] for t in transitions]
        _log(f"⚠️  No 'AI Ready' transition for {issue_key}. Available: {available}")
        return None

    # Transition the issue
    resp = httpx.post(
        f"{jira_url.rstrip('/')}/rest/api/3/issue/{issue_key}/transitions",
        json={"transition": {"id": ai_ready["id"]}},
        headers=headers,
        timeout=30.0,
    )
    resp.raise_for_status()
    _log(f"✅ {issue_key} transitioned to AI Ready")
    return issue_key


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------


def scene_1_code_review(page: Page, collector: EventCollector, mr_url: str) -> None:
    """Scene 1: Automated MR code review."""
    _log("🎬 Scene 1: Automated MR Code Review")
    page.goto(mr_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    # Always wait for the OTEL review_complete event
    _log("Waiting for review to complete...")
    _wait_and_refresh(
        page,
        collector,
        "review_complete",
        timeout=180.0,
    )

    # Scroll through the MR to show review comments
    page.wait_for_timeout(2000)
    _scroll_slowly(page, distance=400, steps=6, pause=2.0)

    _log("✅ Scene 1 complete")


def scene_2_copilot_command(page: Page, collector: EventCollector, mr_url: str) -> None:
    """Scene 2: /copilot inline command on the same MR."""
    _log("🎬 Scene 2: /copilot Command")

    # Ensure the review from Scene 1 is fully complete before posting
    if not collector.has_seen("review_complete"):
        _log("Waiting for review to finish before posting /copilot...")
        collector.wait_for("review_complete", timeout=180.0)

    # Navigate to MR if not already there
    if mr_url not in page.url:
        page.goto(mr_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    else:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

    # Scroll to bottom where comment box is
    page.keyboard.press("End")
    page.wait_for_timeout(1000)

    # Find and click the comment textarea
    comment_box = (
        page.locator("textarea#note-body").first
        or page.locator("[data-testid='comment-field']").first
    )
    comment_box.click()
    page.wait_for_timeout(500)

    _slow_type(
        page,
        "textarea#note-body",
        "/copilot fix the SQL injection in database.py by using parameterized queries",
    )
    page.wait_for_timeout(1000)

    # Submit the comment (use :visible to skip hidden dropdown items)
    submit_btn = page.locator("button:has-text('Comment'):visible").first
    submit_btn.click()
    _log("Comment submitted — waiting for agent...")

    _wait_and_refresh(
        page,
        collector,
        "copilot_command_complete",
        timeout=180.0,
    )

    # Show the new commit — scroll to see the latest activity
    _scroll_slowly(page, distance=400, steps=3, pause=2.0)

    _log("✅ Scene 2 complete")


def scene_3_jira_flow(
    page: Page,
    collector: EventCollector,
    jira_board_url: str,
    gitlab_base_url: str,
    project_path: str,
) -> None:
    """Scene 3: Jira → GitLab coding flow."""
    _log("🎬 Scene 3: Jira → GitLab Flow")
    page.goto(jira_board_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    # Transition a story to "AI Ready" via Jira API
    jira_project_key = re.search(r"/projects/([A-Z]+)/", jira_board_url)
    pkey = jira_project_key.group(1) if jira_project_key else "DEMO"
    issue_key = _jira_transition_to_ai_ready(pkey)
    if not issue_key:
        _log("⏸️  Manual step: drag a Jira story to 'AI Ready' column now")

    # Wait for the agent to start coding
    _log("Waiting for coding_task_started...")
    body = collector.wait_for("coding_task_started", timeout=180.0)
    if body:
        _log("Agent started coding — refreshing Jira to show 'In Progress'")
    page.goto(jira_board_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    # Wait for coding to complete
    _log("Waiting for coding_task_complete...")
    body = collector.wait_for("coding_task_complete", timeout=300.0)
    mr_iid = None
    if body:
        # Try to extract mr_iid from the log body
        match = re.search(r"mr_iid['\"]?\s*[:=]\s*(\d+)", body)
        if match:
            mr_iid = match.group(1)
        _log(f"Coding complete — MR IID: {mr_iid or 'unknown'}")

    # Switch to GitLab to show the MR
    if mr_iid:
        new_mr_url = f"{gitlab_base_url}/{project_path}/-/merge_requests/{mr_iid}"
    else:
        new_mr_url = f"{gitlab_base_url}/{project_path}/-/merge_requests"
    page.goto(new_mr_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    _scroll_slowly(page, distance=300, steps=3, pause=2.0)

    # Switch back to Jira to show "In Review"
    page.goto(jira_board_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    _log("✅ Scene 3 complete")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record a demo video via Playwright + embedded OTEL collector.",
    )
    parser.add_argument(
        "--gitlab-mr-url",
        required=True,
        help="Full URL of the GitLab MR for Scenes 1 & 2",
    )
    parser.add_argument(
        "--jira-board-url",
        required=True,
        help="Full URL of the Jira board for Scene 3",
    )
    parser.add_argument(
        "--gitlab-base-url",
        default=None,
        help="GitLab base URL (auto-detected from --gitlab-mr-url if omitted)",
    )
    parser.add_argument(
        "--project-path",
        default=None,
        help="GitLab project path e.g. myorg/copilot-demo (auto-detected if omitted)",
    )
    parser.add_argument(
        "--otel-port",
        type=int,
        default=4317,
        help="Port for the embedded OTEL collector (default: 4317)",
    )
    parser.add_argument(
        "--cdp-url",
        default="http://host.docker.internal:9222",
        help="Chrome DevTools Protocol URL (default: http://host.docker.internal:9222)",
    )
    parser.add_argument(
        "--output-dir",
        default="demo-video",
        help="Directory for recorded video (default: demo-video/)",
    )
    parser.add_argument(
        "--skip-scene",
        action="append",
        type=int,
        default=[],
        help="Skip a scene by number (1, 2, or 3). Can be repeated.",
    )
    args = parser.parse_args()

    # Auto-detect GitLab base URL and project path from MR URL
    # e.g. https://gitlab.com/myorg/copilot-demo/-/merge_requests/1
    gitlab_base_url = args.gitlab_base_url
    project_path = args.project_path
    if not gitlab_base_url or not project_path:
        match = re.match(r"(https?://[^/]+)/(.+?)/-/merge_requests/\d+", args.gitlab_mr_url)
        if match:
            gitlab_base_url = gitlab_base_url or match.group(1)
            project_path = project_path or match.group(2)
        else:
            print(
                "Error: Could not parse GitLab MR URL."
                " Provide --gitlab-base-url and --project-path."
            )
            sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Preflight: ensure Playwright ffmpeg is installed (needed for video recording)
    import subprocess  # noqa: PLC0415

    ffmpeg_path = Path.home() / ".cache" / "ms-playwright" / "ffmpeg-1011" / "ffmpeg-linux"
    if not ffmpeg_path.exists():
        _log("Installing Playwright ffmpeg for video recording...")
        subprocess.run(["playwright", "install", "--with-deps", "ffmpeg"], check=True)

    # Start embedded OTEL collector
    collector = EventCollector()
    otel_server = _start_otel_server(collector, args.otel_port)

    try:
        with sync_playwright() as pw:
            # Resolve CDP websocket URL — needed when connecting via
            # host.docker.internal because Chrome rejects non-localhost
            # Host headers on the HTTP discovery endpoint.
            cdp_url = args.cdp_url
            import httpx as _httpx  # noqa: PLC0415

            try:
                resp = _httpx.get(
                    f"{cdp_url}/json/version",
                    headers={"Host": "localhost"},
                    timeout=5.0,
                )
                ws_url = resp.json().get("webSocketDebuggerUrl", "")
                if ws_url:
                    # Rewrite ws://127.0.0.1:9222 → ws://host.docker.internal:9222
                    from urllib.parse import urlparse, urlunparse  # noqa: PLC0415

                    parsed_cdp = urlparse(cdp_url)
                    parsed_ws = urlparse(ws_url)
                    host = parsed_cdp.hostname
                    port = parsed_cdp.port or 9222
                    ws_url = urlunparse(parsed_ws._replace(netloc=f"{host}:{port}"))
                    cdp_url = ws_url
            except Exception:
                pass  # Fall back to HTTP discovery

            _log(f"Connecting to browser at {cdp_url}...")
            browser: Browser = pw.chromium.connect_over_cdp(cdp_url)

            # Export storage state (cookies) from the existing browser context
            existing_context = browser.contexts[0]
            storage_state = existing_context.storage_state()

            # Create a new recording context with the same auth state
            context: BrowserContext = browser.new_context(
                storage_state=storage_state,
                record_video_dir=str(output_dir),
                record_video_size={"width": 1920, "height": 1080},
                viewport={"width": 1920, "height": 1080},
            )
            page: Page = context.new_page()

            _log("🎬 Recording started")

            if 1 not in args.skip_scene:
                scene_1_code_review(page, collector, args.gitlab_mr_url)
            if 2 not in args.skip_scene:
                scene_2_copilot_command(page, collector, args.gitlab_mr_url)
            if 3 not in args.skip_scene:
                scene_3_jira_flow(
                    page,
                    collector,
                    args.jira_board_url,
                    gitlab_base_url,
                    project_path,
                )

            # Brief pause on final state
            page.wait_for_timeout(3000)

            # Close context to finalize video
            video_path = page.video.path() if page.video else None
            context.close()

            if video_path:
                _log(f"🎥 Video saved: {video_path}")
                _log(f"   Convert to MP4: ffmpeg -i {video_path} -c:v libx264 demo.mp4")
            else:
                _log("⚠️  No video path available")

            _log("🎬 Recording complete!")

    finally:
        otel_server.stop(grace=2)
        _log("OTEL collector stopped")


if __name__ == "__main__":
    main()
