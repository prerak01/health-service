"""Application and command-line entry point for the health service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
from typing import Annotated, Literal
from uuid import UUID, uuid4

import uvicorn
import psycopg
from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
from prometheus_client import make_asgi_app

from health_service.database import (
    create_endpoint as persist_endpoint,
    delete_endpoint as remove_endpoint,
    is_database_ready,
    list_health_check_history as fetch_health_check_history,
    list_endpoints as fetch_endpoints,
    list_state_transitions as fetch_state_transitions,
)
from health_service.scheduler import HealthCheckScheduler


StateName = Literal["pending", "healthy", "unhealthy"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class EndpointCreateRequest(BaseModel):
    """User-provided monitoring configuration for a new endpoint."""

    url: HttpUrl
    check_interval_seconds: Annotated[int, Field(gt=0)]
    expected_status_code: Annotated[int, Field(ge=100, le=599)]


class EndpointResponse(BaseModel):
    """A stored endpoint configuration and its lifecycle metadata."""

    id: UUID
    url: str
    check_interval_seconds: int
    expected_status_code: int
    current_state: StateName
    last_checked_at: datetime | None
    next_check_at: datetime | None
    created_at: datetime


class HealthCheckHistoryResponse(BaseModel):
    """A persisted result from one endpoint health check."""

    id: UUID
    endpoint_id: UUID
    checked_at: datetime
    status_code: int | None
    latency_ms: int
    success: bool
    error: str | None


class StateTransitionResponse(BaseModel):
    """A persisted endpoint state transition event."""

    id: UUID
    endpoint_id: UUID
    changed_at: datetime
    from_state: StateName | None
    to_state: StateName


def _database_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="database unavailable",
    )


def _normalize_time_range(start_time: datetime, end_time: datetime) -> tuple[datetime, datetime]:
    """Validate and normalize an inclusive, timezone-aware UTC time range."""
    if start_time.tzinfo is None or start_time.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_time must include a timezone",
        )
    if end_time.tzinfo is None or end_time.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_time must include a timezone",
        )

    normalized_start = start_time.astimezone(UTC)
    normalized_end = end_time.astimezone(UTC)
    if normalized_start > normalized_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_time must be before or equal to end_time",
        )
    return normalized_start, normalized_end


def create_app(*, test_run: bool = False, enable_scheduler: bool = True) -> FastAPI:
    """Create the application with its process-level configuration."""
    scheduler = HealthCheckScheduler() if enable_scheduler else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()

    app = FastAPI(title="Health Service", lifespan=lifespan)
    app.mount("/metrics", make_asgi_app())

    @app.get("/health")
    def health() -> dict[str, str | bool]:
        """Report process health and whether this is a test run."""
        return {"status": "ok", "test_run": test_run}

    @app.get("/ready")
    def ready() -> JSONResponse:
        """Report whether the service can reach its PostgreSQL dependency."""
        if is_database_ready():
            return JSONResponse(
                status_code=200,
                content={"status": "ready", "database": "connected"},
            )
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable"},
        )

    @app.post(
        "/endpoints",
        response_model=EndpointResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_registered_endpoint(request: EndpointCreateRequest) -> dict[str, object]:
        """Register and persist an endpoint to be monitored in the future."""
        try:
            return persist_endpoint(
                endpoint_id=uuid4(),
                transition_id=uuid4(),
                url=str(request.url),
                check_interval_seconds=request.check_interval_seconds,
                expected_status_code=request.expected_status_code,
                created_at=datetime.now(UTC),
            )
        except (OSError, ValueError, psycopg.Error) as error:
            raise _database_unavailable() from error

    @app.get("/endpoints", response_model=list[EndpointResponse])
    def list_registered_endpoints() -> list[dict[str, object]]:
        """List every registered endpoint in creation order."""
        try:
            return fetch_endpoints()
        except (OSError, ValueError, psycopg.Error) as error:
            raise _database_unavailable() from error

    @app.delete("/endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_registered_endpoint(endpoint_id: UUID) -> Response:
        """Remove a registered endpoint."""
        try:
            deleted = remove_endpoint(endpoint_id)
        except (OSError, ValueError, psycopg.Error) as error:
            raise _database_unavailable() from error
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/endpoints/{endpoint_id}/history",
        response_model=list[HealthCheckHistoryResponse],
    )
    def get_endpoint_history(
        endpoint_id: UUID,
        start_time: datetime = Query(..., description="Inclusive range start."),
        end_time: datetime = Query(..., description="Inclusive range end."),
    ) -> list[dict[str, object]]:
        """Return health-check results for an endpoint in a time range."""
        normalized_start, normalized_end = _normalize_time_range(start_time, end_time)
        try:
            return fetch_health_check_history(
                endpoint_id,
                start_time=normalized_start,
                end_time=normalized_end,
            )
        except LookupError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="endpoint not found",
            ) from error
        except (OSError, ValueError, psycopg.Error) as error:
            raise _database_unavailable() from error

    @app.get(
        "/endpoints/{endpoint_id}/transitions",
        response_model=list[StateTransitionResponse],
    )
    def get_endpoint_state_transitions(
        endpoint_id: UUID,
        start_time: datetime = Query(..., description="Inclusive range start."),
        end_time: datetime = Query(..., description="Inclusive range end."),
    ) -> list[dict[str, object]]:
        """Return state transitions for an endpoint in a time range."""
        normalized_start, normalized_end = _normalize_time_range(start_time, end_time)
        try:
            return fetch_state_transitions(
                endpoint_id,
                start_time=normalized_start,
                end_time=normalized_end,
            )
        except LookupError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="endpoint not found",
            ) from error
        except (OSError, ValueError, psycopg.Error) as error:
            raise _database_unavailable() from error

    return app


app = create_app()


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line configuration."""
    parser = argparse.ArgumentParser(description="Run the health service.")
    parser.add_argument(
        "--test-run",
        action="store_true",
        help="Report test_run=true from the health endpoint.",
    )
    return parser.parse_args(args)


def run(args: Sequence[str] | None = None) -> None:
    """Start the HTTP service."""
    options = parse_args(args)
    host = os.environ.get("HEALTH_SERVICE_HOST", DEFAULT_HOST)
    port = int(os.environ.get("HEALTH_SERVICE_PORT", str(DEFAULT_PORT)))
    if not 1 <= port <= 65535:
        raise ValueError("HEALTH_SERVICE_PORT must be between 1 and 65535")
    uvicorn.run(create_app(test_run=options.test_run), host=host, port=port)


if __name__ == "__main__":
    run()
