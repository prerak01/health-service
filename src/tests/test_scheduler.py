from datetime import UTC, datetime
from threading import Event, Lock
from uuid import UUID, uuid4

from urllib.error import HTTPError

import pytest
from prometheus_client import REGISTRY

from health_service.scheduler import (
    HealthCheckResult,
    HealthCheckScheduler,
    TokenBucketRateLimiter,
    execute_health_check,
)


ENDPOINT_ID = UUID("e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8")
CHECKED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback) -> None:
        return None


class UnlimitedRateLimiter:
    def acquire(self, url: str, stop_event: Event) -> float:
        return 0.0


def endpoint(endpoint_id: UUID = ENDPOINT_ID) -> dict[str, object]:
    return {
        "id": endpoint_id,
        "url": "https://example.com/health",
        "check_interval_seconds": 30,
        "expected_status_code": 200,
    }


def result(endpoint_id: UUID = ENDPOINT_ID, *, success: bool = True) -> HealthCheckResult:
    return HealthCheckResult(
        id=uuid4(),
        endpoint_id=endpoint_id,
        checked_at=CHECKED_AT,
        status_code=200 if success else 503,
        latency_ms=12,
        success=success,
        error=None,
    )


def no_response_result(endpoint_id: UUID = ENDPOINT_ID) -> HealthCheckResult:
    return HealthCheckResult(
        id=uuid4(),
        endpoint_id=endpoint_id,
        checked_at=CHECKED_AT,
        status_code=None,
        latency_ms=12,
        success=False,
        error="TimeoutError: request timed out",
    )


def metric_value(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels=labels) or 0.0


def test_execute_health_check_records_a_matching_status_code() -> None:
    calls: list[tuple[str, float]] = []

    def opener(request, *, timeout):
        calls.append((request.full_url, timeout))
        return FakeResponse(200)

    check = execute_health_check(
        endpoint(endpoint_id=ENDPOINT_ID),
        opener=opener,
        monotonic_clock=iter([10.0, 10.123]).__next__,
        now_provider=lambda: CHECKED_AT,
    )

    assert check.endpoint_id == ENDPOINT_ID
    assert check.status_code == 200
    assert check.latency_ms == 123
    assert check.success is True
    assert check.error is None
    assert calls == [("https://example.com/health", 2.0)]


def test_execute_health_check_marks_a_wrong_status_unhealthy() -> None:
    check = execute_health_check(
        endpoint(),
        opener=lambda request, *, timeout: FakeResponse(503),
        now_provider=lambda: CHECKED_AT,
    )

    assert check.status_code == 503
    assert check.success is False
    assert check.error is None


def test_execute_health_check_preserves_http_error_status() -> None:
    def opener(request, *, timeout):
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    check = execute_health_check(
        endpoint(),
        opener=opener,
        now_provider=lambda: CHECKED_AT,
    )

    assert check.status_code == 503
    assert check.success is False
    assert check.error is None


def test_execute_health_check_records_request_failures() -> None:
    def opener(request, *, timeout):
        raise TimeoutError("request timed out")

    check = execute_health_check(
        endpoint(),
        opener=opener,
        now_provider=lambda: CHECKED_AT,
    )

    assert check.status_code is None
    assert check.success is False
    assert check.error == "TimeoutError: request timed out"


def test_token_bucket_allows_initial_burst_then_refills() -> None:
    current_time = 100.0
    waits: list[float] = []

    def wait(stop_event: Event, delay: float) -> bool:
        nonlocal current_time
        waits.append(delay)
        current_time += delay
        return stop_event.is_set()

    limiter = TokenBucketRateLimiter(
        10,
        monotonic_clock=lambda: current_time,
        waiter=wait,
    )
    stop_event = Event()

    immediate_waits = [
        limiter.acquire("https://example.com/health", stop_event)
        for _ in range(10)
    ]
    throttled_wait = limiter.acquire("https://example.com/ready", stop_event)

    assert immediate_waits == [0.0] * 10
    assert throttled_wait == pytest.approx(0.1)
    assert waits == pytest.approx([0.1])


def test_token_bucket_groups_by_normalized_destination() -> None:
    current_time = 10.0
    waits: list[float] = []

    def wait(stop_event: Event, delay: float) -> bool:
        nonlocal current_time
        waits.append(delay)
        current_time += delay
        return False

    limiter = TokenBucketRateLimiter(
        1,
        monotonic_clock=lambda: current_time,
        waiter=wait,
    )
    stop_event = Event()

    assert limiter.acquire("https://EXAMPLE.com/health", stop_event) == 0.0
    assert limiter.acquire("https://example.com/ready?full=true", stop_event) == 1.0
    assert limiter.acquire("https://example.com:8443/health", stop_event) == 0.0
    assert limiter.acquire("https://other.example/health", stop_event) == 0.0
    assert waits == [1.0]


def test_token_bucket_uses_effective_http_and_https_ports() -> None:
    assert TokenBucketRateLimiter.destination_key("https://example.com/a") == (
        "example.com",
        443,
    )
    assert TokenBucketRateLimiter.destination_key("http://example.com/a") == (
        "example.com",
        80,
    )
    assert TokenBucketRateLimiter.destination_key("https://example.com:9443/a") == (
        "example.com",
        9443,
    )


def test_token_bucket_wait_is_cancelled_by_shutdown() -> None:
    stop_event = Event()
    limiter = TokenBucketRateLimiter(
        1,
        monotonic_clock=lambda: 10.0,
        waiter=lambda current_stop_event, delay: current_stop_event.wait(0),
    )

    assert limiter.acquire("https://example.com/first", stop_event) == 0.0
    stop_event.set()
    assert limiter.acquire("https://example.com/second", stop_event) is None


def test_scheduler_skips_ongoing_endpoints_and_reserves_before_submit() -> None:
    first_endpoint = endpoint()
    second_id = UUID("c6f8e4bb-5e6f-46e6-9d70-b4c9f9532f9c")
    second_endpoint = endpoint(second_id)
    checked_ids: list[UUID] = []
    persisted_ids: list[UUID] = []

    def check_function(current_endpoint):
        current_id = current_endpoint["id"]
        assert current_id in scheduler.ongoing_check_ids
        checked_ids.append(current_id)
        return result(current_id)

    def persist_result(**values):
        persisted_ids.append(values["endpoint_id"])

    scheduler = HealthCheckScheduler(
        schema_initializer=lambda: None,
        due_endpoint_loader=lambda now: [first_endpoint, second_endpoint],
        check_function=check_function,
        result_persister=persist_result,
    )
    try:
        assert scheduler._reserve(first_endpoint["id"])
        scheduler.run_once(CHECKED_AT)
        scheduler._release(first_endpoint["id"])
    finally:
        scheduler.stop()

    assert checked_ids == [second_id]
    assert persisted_ids == [second_id]
    assert scheduler.ongoing_check_ids == frozenset()


def test_scheduler_records_checks_with_and_without_responses() -> None:
    response_before = metric_value(
        "health_service_health_checks_total",
        {"outcome": "response"},
    )
    no_response_before = metric_value(
        "health_service_health_checks_total",
        {"outcome": "no_response"},
    )

    responding_scheduler = HealthCheckScheduler(
        schema_initializer=lambda: None,
        due_endpoint_loader=lambda now: [],
        check_function=lambda current_endpoint: result(success=False),
        result_persister=lambda **values: None,
    )
    failing_scheduler = HealthCheckScheduler(
        schema_initializer=lambda: None,
        due_endpoint_loader=lambda now: [],
        check_function=lambda current_endpoint: no_response_result(),
        result_persister=lambda **values: None,
    )
    try:
        responding_scheduler._run_endpoint_check(endpoint())
        failing_scheduler._run_endpoint_check(endpoint())
    finally:
        responding_scheduler.stop()
        failing_scheduler.stop()

    assert metric_value(
        "health_service_health_checks_total",
        {"outcome": "response"},
    ) == response_before + 1
    assert metric_value(
        "health_service_health_checks_total",
        {"outcome": "no_response"},
    ) == no_response_before + 1


def test_scheduler_acquires_rate_limit_permit_before_check(caplog) -> None:
    events: list[str] = []
    persisted_ids: list[UUID] = []

    class RecordingRateLimiter:
        def acquire(self, url: str, stop_event: Event) -> float:
            assert url == "https://example.com/health"
            events.append("permit")
            return 0.25

    def check_function(current_endpoint):
        events.append("check")
        return result(current_endpoint["id"])

    scheduler = HealthCheckScheduler(
        schema_initializer=lambda: None,
        due_endpoint_loader=lambda now: [],
        check_function=check_function,
        result_persister=lambda **values: persisted_ids.append(values["endpoint_id"]),
        rate_limiter=RecordingRateLimiter(),
    )
    try:
        with caplog.at_level("INFO", logger="uvicorn.error"):
            scheduler._run_endpoint_check(endpoint())
    finally:
        scheduler.stop()

    assert events == ["permit", "check"]
    assert persisted_ids == [ENDPOINT_ID]
    assert "waited_seconds=0.250" in caplog.text


def test_scheduler_tracks_pending_tasks() -> None:
    started = Event()
    release = Event()
    first_endpoint = endpoint()
    second_endpoint = endpoint(uuid4())
    pending_before = metric_value("health_service_scheduler_tasks_pending")

    def blocking_check(current_endpoint):
        if current_endpoint["id"] == first_endpoint["id"]:
            started.set()
            assert release.wait(timeout=2)
        return result(current_endpoint["id"])

    scheduler = HealthCheckScheduler(
        max_workers=1,
        schema_initializer=lambda: None,
        due_endpoint_loader=lambda now: [first_endpoint, second_endpoint],
        check_function=blocking_check,
        result_persister=lambda **values: None,
    )
    try:
        scheduler.run_once(CHECKED_AT)
        assert started.wait(timeout=2)
        assert metric_value("health_service_scheduler_tasks_pending") == pending_before + 1
    finally:
        release.set()
        scheduler.stop()

    assert metric_value("health_service_scheduler_tasks_pending") == pending_before


def test_scheduler_releases_endpoint_when_worker_fails() -> None:
    current_endpoint = endpoint()

    def fail_check(current_endpoint):
        raise RuntimeError("checker failed")

    scheduler = HealthCheckScheduler(
        schema_initializer=lambda: None,
        due_endpoint_loader=lambda now: [],
        check_function=fail_check,
    )
    try:
        assert scheduler._reserve(current_endpoint["id"])
        scheduler._run_endpoint_check(current_endpoint)
    finally:
        scheduler.stop()

    assert scheduler.ongoing_check_ids == frozenset()


def test_scheduler_uses_at_most_fifty_workers() -> None:
    all_workers_started = Event()
    release_workers = Event()
    active_lock = Lock()
    active_workers = 0
    maximum_active_workers = 0
    due_endpoints = [endpoint(uuid4()) for _ in range(51)]

    def blocking_check(current_endpoint):
        nonlocal active_workers, maximum_active_workers
        with active_lock:
            active_workers += 1
            maximum_active_workers = max(maximum_active_workers, active_workers)
            if active_workers == 50:
                all_workers_started.set()
        assert release_workers.wait(timeout=2)
        with active_lock:
            active_workers -= 1
        return result(current_endpoint["id"])

    scheduler = HealthCheckScheduler(
        schema_initializer=lambda: None,
        due_endpoint_loader=lambda now: due_endpoints,
        check_function=blocking_check,
        result_persister=lambda **values: None,
        rate_limiter=UnlimitedRateLimiter(),
    )
    try:
        scheduler.run_once(CHECKED_AT)
        assert all_workers_started.wait(timeout=2)
        assert maximum_active_workers == 50
    finally:
        release_workers.set()
        scheduler.stop()


def test_scheduler_stop_waits_for_a_running_check() -> None:
    started = Event()
    release = Event()
    current_endpoint = endpoint()

    def blocking_check(current_endpoint):
        started.set()
        assert release.wait(timeout=2)
        return result()

    scheduler = HealthCheckScheduler(
        schema_initializer=lambda: None,
        due_endpoint_loader=lambda now: [current_endpoint],
        check_function=blocking_check,
        result_persister=lambda **values: None,
    )
    scheduler.run_once(CHECKED_AT)
    assert started.wait(timeout=2)

    release.set()
    scheduler.stop()

    assert scheduler.ongoing_check_ids == frozenset()


def test_scheduler_stop_cancels_a_rate_limited_check() -> None:
    permit_requested = Event()
    checks: list[object] = []
    current_endpoint = endpoint()

    class BlockingRateLimiter:
        def acquire(self, url: str, stop_event: Event) -> None:
            permit_requested.set()
            assert stop_event.wait(timeout=2)
            return None

    scheduler = HealthCheckScheduler(
        schema_initializer=lambda: None,
        due_endpoint_loader=lambda now: [current_endpoint],
        check_function=lambda value: checks.append(value),
        result_persister=lambda **values: None,
        rate_limiter=BlockingRateLimiter(),
    )
    scheduler.run_once(CHECKED_AT)
    assert permit_requested.wait(timeout=2)

    scheduler.stop()

    assert checks == []
    assert scheduler.ongoing_check_ids == frozenset()
