"""Health-check execution and scheduling for registered endpoints."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from health_service.database import (
    initialize_schema,
    list_due_endpoints,
    record_health_check,
)
from health_service.metrics import (
    SCHEDULER_TASKS_PENDING,
    record_health_check as record_health_check_metric,
)


logger = logging.getLogger(__name__)
scheduler_logger = logging.getLogger("uvicorn.error")

SCHEDULER_INTERVAL_SECONDS = 5.0
HEALTH_CHECK_TIMEOUT_SECONDS = 2.0
MAX_WORKERS = 50
DEFAULT_OUTBOUND_RATE_LIMIT_RPS = 10

EndpointRecord = Mapping[str, object]
NowProvider = Callable[[], datetime]
DueEndpointLoader = Callable[[datetime], list[EndpointRecord]]
SchemaInitializer = Callable[[], None]
HealthCheckPersister = Callable[..., object]
HealthCheckFunction = Callable[[EndpointRecord], "HealthCheckResult"]
RateLimitWaiter = Callable[[Event, float], bool]


class OutboundRateLimiter(Protocol):
    """Wait for permission to start an outbound request."""

    def acquire(self, url: str, stop_event: Event) -> float | None:
        """Return seconds waited, or None when shutdown cancels the wait."""
        ...


@dataclass(slots=True)
class _TokenBucket:
    tokens: float
    last_refill: float
    lock: Lock


class TokenBucketRateLimiter:
    """Apply a token-bucket request-start limit per destination."""

    def __init__(
        self,
        requests_per_second: int = DEFAULT_OUTBOUND_RATE_LIMIT_RPS,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
        waiter: RateLimitWaiter | None = None,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be greater than zero")
        self.requests_per_second = requests_per_second
        self.capacity = float(requests_per_second)
        self._monotonic_clock = monotonic_clock
        self._waiter = waiter or (lambda stop_event, delay: stop_event.wait(delay))
        self._buckets: dict[tuple[str, int], _TokenBucket] = {}
        self._buckets_lock = Lock()

    @staticmethod
    def destination_key(url: str) -> tuple[str, int]:
        """Return normalized hostname and effective port for an HTTP URL."""
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if hostname is None or scheme not in {"http", "https"}:
            raise ValueError(f"unsupported health-check URL: {url}")
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80
        return hostname.lower(), port

    def acquire(self, url: str, stop_event: Event) -> float | None:
        """Consume one token, waiting interruptibly for a refill when needed."""
        started_at = self._monotonic_clock()
        bucket = self._bucket_for(self.destination_key(url), started_at)
        waited_seconds = 0.0

        while True:
            with bucket.lock:
                now = self._monotonic_clock()
                elapsed = max(0.0, now - bucket.last_refill)
                bucket.tokens = min(
                    self.capacity,
                    bucket.tokens + elapsed * self.requests_per_second,
                )
                bucket.last_refill = now
                # Treat sub-nanotoken rounding differences as a full token so
                # a completed wait cannot spin forever at the boundary.
                if bucket.tokens >= 1.0 - 1e-9:
                    bucket.tokens = max(0.0, bucket.tokens - 1.0)
                    return waited_seconds
                delay = (1.0 - bucket.tokens) / self.requests_per_second

            wait_started_at = self._monotonic_clock()
            if self._waiter(stop_event, delay):
                return None
            waited_seconds += max(0.0, self._monotonic_clock() - wait_started_at)

    def _bucket_for(self, key: tuple[str, int], now: float) -> _TokenBucket:
        with self._buckets_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _TokenBucket(
                    tokens=self.capacity,
                    last_refill=now,
                    lock=Lock(),
                )
                self._buckets[key] = bucket
            return bucket


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    """The result of one endpoint health check."""

    id: UUID
    endpoint_id: UUID
    checked_at: datetime
    status_code: int | None
    latency_ms: int
    success: bool
    error: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_error(error: Exception) -> str:
    message = str(error)
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def execute_health_check(
    endpoint: EndpointRecord,
    *,
    opener=urlopen,
    monotonic_clock: Callable[[], float] = monotonic,
    now_provider: NowProvider | None = None,
) -> HealthCheckResult:
    """Perform a GET request and convert its outcome into a result record."""
    endpoint_id = cast(UUID, endpoint["id"])
    expected_status_code = int(endpoint["expected_status_code"])
    start = monotonic_clock()
    status_code: int | None = None
    error: str | None = None

    try:
        request = Request(str(endpoint["url"]), method="GET")
        with opener(request, timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as response:
            response_status = getattr(response, "status", None)
            status_code = int(
                response_status if response_status is not None else response.getcode()
            )
    except HTTPError as request_error:
        # HTTPError is also a valid HTTP response and must retain its status code.
        status_code = request_error.code
    except Exception as request_error:
        error = _format_error(request_error)

    latency_ms = max(0, int(round((monotonic_clock() - start) * 1000)))
    checked_at = (now_provider or _utc_now)()
    success = error is None and status_code == expected_status_code

    return HealthCheckResult(
        id=uuid4(),
        endpoint_id=endpoint_id,
        checked_at=checked_at,
        status_code=status_code,
        latency_ms=latency_ms,
        success=success,
        error=error,
    )


class HealthCheckScheduler:
    """Run due endpoint checks on a bounded worker pool."""

    def __init__(
        self,
        *,
        scan_interval_seconds: float = SCHEDULER_INTERVAL_SECONDS,
        max_workers: int = MAX_WORKERS,
        schema_initializer: SchemaInitializer = initialize_schema,
        due_endpoint_loader: DueEndpointLoader = list_due_endpoints,
        check_function: HealthCheckFunction = execute_health_check,
        result_persister: HealthCheckPersister = record_health_check,
        rate_limiter: OutboundRateLimiter | None = None,
        now_provider: NowProvider | None = None,
    ) -> None:
        self.scan_interval_seconds = scan_interval_seconds
        self.max_workers = max_workers
        self._schema_initializer = schema_initializer
        self._due_endpoint_loader = due_endpoint_loader
        self._check_function = check_function
        self._result_persister = result_persister
        self._rate_limiter = (
            rate_limiter if rate_limiter is not None else TokenBucketRateLimiter()
        )
        self._now_provider = now_provider or _utc_now
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._stop_event = Event()
        self._ongoing_check_ids: set[object] = set()
        self._ongoing_lock = Lock()
        self._thread: Thread | None = None
        self._lifecycle_lock = Lock()
        self._started = False
        self._stopped = False

    @property
    def ongoing_check_ids(self) -> frozenset[object]:
        """Return a snapshot of currently reserved endpoint IDs."""
        with self._ongoing_lock:
            return frozenset(self._ongoing_check_ids)

    def start(self) -> None:
        """Start the scheduler thread."""
        with self._lifecycle_lock:
            if self._started:
                return
            if self._stopped:
                raise RuntimeError("scheduler cannot be restarted")
            self._started = True
            self._thread = Thread(
                target=self._run_loop,
                name="health-check-scheduler",
                daemon=False,
            )
            self._thread.start()
            scheduler_logger.info(
                "health-check scheduler started: scan_interval_seconds=%s max_workers=%s",
                self.scan_interval_seconds,
                self.max_workers,
            )

    def stop(self) -> None:
        """Stop scheduling and wait for queued and in-flight checks."""
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop_event.set()
            thread = self._thread

        if thread is not None:
            thread.join()
        self._executor.shutdown(wait=True)
        scheduler_logger.info("health-check scheduler stopped")

    def run_once(self, now: datetime | None = None) -> None:
        """Run one scheduler scan; exposed for deterministic unit tests."""
        scan_time = now or self._now_provider()
        try:
            self._schema_initializer()
            due_endpoints = self._due_endpoint_loader(scan_time)
        except Exception:
            logger.exception("health-check scheduler scan failed")
            return

        scheduled_count = 0
        skipped_count = 0
        for endpoint in due_endpoints:
            endpoint_id = endpoint["id"]
            if not self._reserve(endpoint_id):
                skipped_count += 1
                scheduler_logger.info(
                    "health-check scheduler skipped endpoint %s: check already in progress",
                    endpoint_id,
                )
                continue
            SCHEDULER_TASKS_PENDING.inc()
            try:
                self._executor.submit(self._run_queued_endpoint_check, endpoint)
                scheduled_count += 1
                scheduler_logger.info("health check scheduled for endpoint %s", endpoint_id)
            except RuntimeError:
                # This can happen if shutdown races with a scan.
                SCHEDULER_TASKS_PENDING.dec()
                self._release(endpoint_id)
                logger.exception("unable to submit health check for %s", endpoint_id)

        scheduler_logger.info(
            "health-check scheduler scan completed: due=%s scheduled=%s skipped=%s",
            len(due_endpoints),
            scheduled_count,
            skipped_count,
        )

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self.scan_interval_seconds):
            self.run_once()

    def _reserve(self, endpoint_id: object) -> bool:
        """Reserve an endpoint exactly once before submitting its work."""
        with self._ongoing_lock:
            if endpoint_id in self._ongoing_check_ids:
                return False
            self._ongoing_check_ids.add(endpoint_id)
            return True

    def _release(self, endpoint_id: object) -> None:
        with self._ongoing_lock:
            self._ongoing_check_ids.discard(endpoint_id)

    def _run_queued_endpoint_check(self, endpoint: EndpointRecord) -> None:
        """Start a queued check and remove it from the pending count."""
        SCHEDULER_TASKS_PENDING.dec()
        self._run_endpoint_check(endpoint)

    def _run_endpoint_check(self, endpoint: EndpointRecord) -> None:
        endpoint_id = endpoint["id"]
        try:
            waited_seconds = self._rate_limiter.acquire(
                str(endpoint["url"]),
                self._stop_event,
            )
            if waited_seconds is None:
                scheduler_logger.info(
                    "health check cancelled while rate limited for endpoint %s",
                    endpoint_id,
                )
                return
            if waited_seconds > 0:
                scheduler_logger.info(
                    "health check rate limited for endpoint %s: waited_seconds=%.3f",
                    endpoint_id,
                    waited_seconds,
                )
            result = self._check_function(endpoint)
            record_health_check_metric(result.status_code)
            outcome = "succeeded" if result.success else "failed"
            scheduler_logger.info(
                "health check %s for endpoint %s: status_code=%s latency_ms=%s error=%s",
                outcome,
                endpoint_id,
                result.status_code,
                result.latency_ms,
                result.error,
            )

            fallback_next_check_at = result.checked_at + timedelta(
                seconds=int(endpoint["check_interval_seconds"])
            )
            persisted_endpoint = self._result_persister(
                result_id=result.id,
                endpoint_id=result.endpoint_id,
                checked_at=result.checked_at,
                status_code=result.status_code,
                latency_ms=result.latency_ms,
                success=result.success,
                error=result.error,
                check_interval_seconds=int(endpoint["check_interval_seconds"]),
            )
            next_check_at = fallback_next_check_at
            if isinstance(persisted_endpoint, Mapping):
                persisted_next_check_at = persisted_endpoint.get("next_check_at")
                if persisted_next_check_at is not None:
                    next_check_at = persisted_next_check_at
            scheduler_logger.info(
                "next health check scheduled for endpoint %s at %s",
                endpoint_id,
                next_check_at,
            )
        except Exception:
            logger.exception("health check failed for endpoint %s", endpoint_id)
        finally:
            self._release(endpoint_id)
