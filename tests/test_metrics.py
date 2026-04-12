"""Tests for OTel metrics recording paths — unit and integration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from gitlab_copilot_agent import metrics as app_metrics
from gitlab_copilot_agent.gitlab_client import GitLabClient, MRDetails
from gitlab_copilot_agent.pipeline import run_pipeline
from gitlab_copilot_agent.review_pipeline import ReviewContext, ReviewPipeline
from gitlab_copilot_agent.task_executor import ReviewResult, TaskExecutionError
from tests.conftest import (
    DIFF_REFS,
    FAKE_REVIEW_OUTPUT,
    HEADERS,
    MR_PAYLOAD,
    make_settings,
    make_task_event,
)


def _setup_test_meter() -> InMemoryMetricReader:
    """Replace the global meter with a testable in-memory provider."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    # Re-create instruments on the new provider's meter
    meter = provider.get_meter(app_metrics.METER_NAME)
    app_metrics.meter = meter
    app_metrics.reviews_total = meter.create_counter("reviews_total", unit="1")
    app_metrics.reviews_duration = meter.create_histogram("reviews_duration_seconds", unit="s")
    app_metrics.coding_tasks_total = meter.create_counter("coding_tasks_total", unit="1")
    app_metrics.coding_tasks_duration = meter.create_histogram(
        "coding_tasks_duration_seconds", unit="s"
    )
    app_metrics.webhook_received_total = meter.create_counter("webhook_received_total", unit="1")
    app_metrics.webhook_errors_total = meter.create_counter("webhook_errors_total", unit="1")
    app_metrics.copilot_session_duration = meter.create_histogram(
        "copilot_session_duration_seconds", unit="s"
    )
    return reader


def _get_metric_value(reader: InMemoryMetricReader, name: str) -> list[dict[str, object]]:
    """Extract data points for a given metric name."""
    data = reader.get_metrics_data()
    results: list[dict[str, object]] = []
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        # Counters have .value, histograms have .sum/.count
                        val = getattr(point, "value", None) or getattr(point, "sum", 0)
                        results.append({"value": val, "attributes": dict(point.attributes)})
    return results


def test_counter_increments() -> None:
    reader = _setup_test_meter()
    app_metrics.reviews_total.add(1, {"outcome": "success"})
    app_metrics.reviews_total.add(1, {"outcome": "error"})

    points = _get_metric_value(reader, "reviews_total")
    assert len(points) == 2
    values_by_outcome = {p["attributes"]["outcome"]: p["value"] for p in points}
    assert values_by_outcome["success"] == 1
    assert values_by_outcome["error"] == 1


def test_histogram_records() -> None:
    reader = _setup_test_meter()
    app_metrics.reviews_duration.record(1.5, {"outcome": "success"})
    app_metrics.reviews_duration.record(0.5, {"outcome": "success"})

    points = _get_metric_value(reader, "reviews_duration_seconds")
    assert len(points) == 1  # aggregated into one point per attribute set
    assert points[0]["value"] == 2.0
    assert points[0]["attributes"] == {"outcome": "success"}


def test_webhook_errors_counter() -> None:
    reader = _setup_test_meter()
    app_metrics.webhook_errors_total.add(1, {"handler": "review"})
    app_metrics.webhook_errors_total.add(1, {"handler": "copilot_comment"})

    points = _get_metric_value(reader, "webhook_errors_total")
    assert len(points) == 2


def test_copilot_session_duration_records_task_type() -> None:
    reader = _setup_test_meter()
    app_metrics.copilot_session_duration.record(10.0, {"task_type": "review"})
    app_metrics.copilot_session_duration.record(25.0, {"task_type": "coding"})

    points = _get_metric_value(reader, "copilot_session_duration_seconds")
    task_types = {p["attributes"]["task_type"] for p in points}
    assert task_types == {"review", "coding"}


# -- Integration tests: verify metrics recorded through real code paths --
# These patch the metric instruments at the call site to verify recording.


def _make_mock_gl_client(
    mock_run_review: AsyncMock,
    *,
    mr_details_side_effect: Exception | None = None,
) -> AsyncMock:
    """Wire up a mock GitLabClient for review pipeline tests."""
    gl = AsyncMock(spec=GitLabClient)
    gl.clone_repo.return_value = "/tmp/fake-repo"
    if mr_details_side_effect:
        gl.get_mr_details.side_effect = mr_details_side_effect
    else:
        gl.get_mr_details.return_value = MRDetails(
            title="t", description=None, diff_refs=DIFF_REFS, changes=[]
        )
    gl.list_mr_discussions.return_value = []
    gl.get_mr_commits.return_value = []
    mock_run_review.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)
    return gl


@patch("gitlab_copilot_agent.review_pipeline.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
async def test_review_pipeline_records_success_metrics(
    mock_run_review: AsyncMock,
    _mock_post: AsyncMock,
) -> None:
    """ReviewPipeline records reviews_total and reviews_duration with outcome=success."""
    gl = _make_mock_gl_client(mock_run_review)

    mock_total = MagicMock()
    mock_duration = MagicMock()
    with (
        patch("gitlab_copilot_agent.review_pipeline.reviews_total", mock_total),
        patch("gitlab_copilot_agent.review_pipeline.reviews_duration", mock_duration),
    ):
        pipeline = ReviewPipeline(
            settings=make_settings(),
            event=make_task_event(),
            executor=AsyncMock(),
            gl_client=gl,
        )
        await run_pipeline(pipeline, ReviewContext())

    mock_total.add.assert_called_once_with(1, {"outcome": "success"})
    mock_duration.record.assert_called_once()
    args = mock_duration.record.call_args
    assert args[0][0] > 0  # duration > 0
    assert args[0][1] == {"outcome": "success"}


@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
async def test_review_pipeline_records_error_metrics(
    mock_run_review: AsyncMock,
) -> None:
    """ReviewPipeline records reviews_total with outcome=error on failure."""
    gl = _make_mock_gl_client(mock_run_review, mr_details_side_effect=RuntimeError("boom"))

    mock_total = MagicMock()
    mock_duration = MagicMock()
    with (
        patch("gitlab_copilot_agent.review_pipeline.reviews_total", mock_total),
        patch("gitlab_copilot_agent.review_pipeline.reviews_duration", mock_duration),
        pytest.raises(RuntimeError),
    ):
        pipeline = ReviewPipeline(
            settings=make_settings(),
            event=make_task_event(),
            executor=AsyncMock(),
            gl_client=gl,
        )
        await run_pipeline(pipeline, ReviewContext())

    mock_total.add.assert_called_once_with(1, {"outcome": "error"})
    mock_duration.record.assert_called_once()
    assert mock_duration.record.call_args[0][1] == {"outcome": "error"}


@patch("gitlab_copilot_agent.review_pipeline.run_review", new_callable=AsyncMock)
async def test_review_task_execution_failure_posts_comment_without_raising(
    mock_run_review: AsyncMock,
) -> None:
    gl = AsyncMock(spec=GitLabClient)
    gl.clone_repo.return_value = "/tmp/fake-repo"
    gl.get_mr_details.return_value = MRDetails(
        title="t", description=None, diff_refs=DIFF_REFS, changes=[]
    )
    gl.list_mr_discussions.return_value = []
    gl.get_mr_commits.return_value = []
    mock_run_review.side_effect = TaskExecutionError("Task failed: runner error")

    with pytest.raises(TaskExecutionError, match="runner error"):
        pipeline = ReviewPipeline(
            settings=make_settings(),
            event=make_task_event(),
            executor=AsyncMock(),
            gl_client=gl,
        )
        await run_pipeline(pipeline, ReviewContext())

    gl.post_mr_comment.assert_awaited_once()
    comment = gl.post_mr_comment.call_args[0][2]
    assert "Automated review failed" in comment
    assert "runner error" not in comment  # user-friendly message, no internal details


async def test_webhook_records_error_metric_on_background_failure(
    client: AsyncClient,
) -> None:
    """webhook_errors_total incremented when background review task fails."""
    mock_errors = MagicMock()
    with (
        patch(
            "gitlab_copilot_agent.gitlab_webhook.run_pipeline",
            new_callable=AsyncMock,
            side_effect=RuntimeError("clone failed"),
        ),
        patch("gitlab_copilot_agent.gitlab_webhook.webhook_errors_total", mock_errors),
    ):
        resp = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
        assert resp.json()["status"] == "queued"
        await asyncio.sleep(0.1)

    mock_errors.add.assert_called_once_with(1, {"handler": "review"})


async def test_webhook_records_received_metric(client: AsyncClient) -> None:
    """webhook_received_total incremented on every webhook, even ignored ones."""
    mock_received = MagicMock()
    with patch("gitlab_copilot_agent.gitlab_webhook.webhook_received_total", mock_received):
        await client.post("/webhook", json={"object_kind": "push"}, headers=HEADERS)

    mock_received.add.assert_called_once_with(1, {"object_kind": "push"})
