"""Shared OTel metrics instruments for the service."""

from opentelemetry import metrics

METER_NAME = "gitlab_copilot_agent"

meter = metrics.get_meter(METER_NAME)

# Service metrics
reviews_total = meter.create_counter(
    name="reviews_total",
    description="Total MR reviews processed",
    unit="1",
)

reviews_duration = meter.create_histogram(
    name="reviews_duration_seconds",
    description="Duration of MR review processing",
    unit="s",
)

coding_tasks_total = meter.create_counter(
    name="coding_tasks_total",
    description="Total coding tasks processed",
    unit="1",
)

coding_tasks_duration = meter.create_histogram(
    name="coding_tasks_duration_seconds",
    description="Duration of coding task processing",
    unit="s",
)

webhook_received_total = meter.create_counter(
    name="webhook_received_total",
    description="Total webhooks received",
    unit="1",
)

webhook_errors_total = meter.create_counter(
    name="webhook_errors_total",
    description="Webhook background processing errors",
    unit="1",
)

# Copilot session metrics
copilot_session_duration = meter.create_histogram(
    name="copilot_session_duration_seconds",
    description="Duration of Copilot SDK session (review or coding)",
    unit="s",
)

# Sandbox metrics
sandbox_duration = meter.create_histogram(
    name="sandbox_duration_seconds",
    description="Duration of sandbox CLI execution",
    unit="s",
)

sandbox_active = meter.create_up_down_counter(
    name="sandbox_active",
    description="Currently active sandbox sessions",
    unit="1",
)

sandbox_outcome_total = meter.create_counter(
    name="sandbox_outcome_total",
    description="Sandbox session outcomes",
    unit="1",
)
