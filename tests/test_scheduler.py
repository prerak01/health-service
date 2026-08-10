from datetime import UTC, datetime
from threading import Event, Lock
from uuid import UUID, uuid4

from urllib.error import HTTPError

from health_service.scheduler import (
    HealthCheckResult,
    HealthCheckScheduler,
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
