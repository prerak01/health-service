"""Prometheus metrics emitted by the health service."""

from prometheus_client import Counter, Gauge


HEALTH_CHECKS_TOTAL = Counter(
    "health_service_health_checks_total",
    "Health checks grouped by whether the endpoint returned an HTTP status code.",
    labelnames=("outcome",),
)

SCHEDULER_TASKS_PENDING = Gauge(
    "health_service_scheduler_tasks_pending",
    "Tasks submitted to the scheduler executor that have not started yet.",
)


def record_health_check(status_code: int | None) -> None:
    """Record whether a health check received an HTTP response."""
    outcome = "response" if status_code is not None else "no_response"
    HEALTH_CHECKS_TOTAL.labels(outcome=outcome).inc()
