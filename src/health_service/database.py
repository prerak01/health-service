"""PostgreSQL persistence helpers for the health service."""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = (
    "postgresql://health_service:health_service@127.0.0.1:5432/health_service"
)

ENDPOINTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS endpoints (
    id UUID PRIMARY KEY,
    url TEXT NOT NULL,
    check_interval_seconds INTEGER NOT NULL CHECK (check_interval_seconds > 0),
    expected_status_code SMALLINT NOT NULL
        CHECK (expected_status_code BETWEEN 100 AND 599),
    current_state TEXT NOT NULL
        CHECK (current_state IN ('pending', 'healthy', 'unhealthy')),
    last_checked_at TIMESTAMPTZ NULL,
    next_check_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL
)
"""

HEALTH_CHECK_RESULTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS health_check_results (
    id UUID PRIMARY KEY,
    endpoint_id UUID NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    checked_at TIMESTAMPTZ NOT NULL,
    status_code SMALLINT NULL CHECK (status_code BETWEEN 100 AND 599),
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    success BOOLEAN NOT NULL,
    error TEXT NULL
)
"""

HEALTH_CHECK_RESULTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS health_check_results_endpoint_checked_at_idx
ON health_check_results (endpoint_id, checked_at DESC)
"""

ENDPOINT_FIELDS = """
id, url, check_interval_seconds, expected_status_code, current_state,
last_checked_at, next_check_at, created_at
"""


def initialize_endpoint_table() -> None:
    """Create the endpoint table when it has not already been created."""
    with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
        connection.execute(ENDPOINTS_TABLE_SQL)


def initialize_schema() -> None:
    """Create all tables and indexes used by the service."""
    with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
        connection.execute(ENDPOINTS_TABLE_SQL)
        connection.execute(HEALTH_CHECK_RESULTS_TABLE_SQL)
        connection.execute(HEALTH_CHECK_RESULTS_INDEX_SQL)


def create_endpoint(
    *,
    endpoint_id: object,
    url: str,
    check_interval_seconds: int,
    expected_status_code: int,
    created_at: object,
) -> dict[str, object]:
    """Persist an endpoint and return its complete stored representation."""
    query = f"""
    INSERT INTO endpoints ({ENDPOINT_FIELDS})
    VALUES (%s, %s, %s, %s, 'pending', NULL, NULL, %s)
    RETURNING {ENDPOINT_FIELDS}
    """
    with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
        connection.execute(ENDPOINTS_TABLE_SQL)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                query,
                (
                    endpoint_id,
                    url,
                    check_interval_seconds,
                    expected_status_code,
                    created_at,
                ),
            )
            endpoint = cursor.fetchone()
    assert endpoint is not None
    return endpoint


def list_endpoints() -> list[dict[str, object]]:
    """Return registered endpoints in their creation order."""
    query = f"SELECT {ENDPOINT_FIELDS} FROM endpoints ORDER BY created_at ASC, id ASC"
    with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
        connection.execute(ENDPOINTS_TABLE_SQL)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query)
            return list(cursor.fetchall())


def list_due_endpoints(now: datetime) -> list[dict[str, object]]:
    """Return endpoints whose next check is due at the supplied UTC time."""
    query = f"""
    SELECT {ENDPOINT_FIELDS}
    FROM endpoints
    WHERE next_check_at IS NULL OR next_check_at <= %s
    ORDER BY next_check_at ASC NULLS FIRST, created_at ASC, id ASC
    """
    with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, (now,))
            return list(cursor.fetchall())


def record_health_check(
    *,
    result_id: object,
    endpoint_id: object,
    checked_at: datetime,
    status_code: int | None,
    latency_ms: int,
    success: bool,
    error: str | None,
    check_interval_seconds: int,
) -> dict[str, object]:
    """Store a result and update its endpoint in one transaction."""
    next_check_at = checked_at + timedelta(seconds=check_interval_seconds)
    current_state = "healthy" if success else "unhealthy"

    result_query = """
    INSERT INTO health_check_results
        (id, endpoint_id, checked_at, status_code, latency_ms, success, error)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    endpoint_query = f"""
    UPDATE endpoints
    SET current_state = %s, last_checked_at = %s, next_check_at = %s
    WHERE id = %s
    RETURNING {ENDPOINT_FIELDS}
    """

    with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
        connection.execute(ENDPOINTS_TABLE_SQL)
        connection.execute(HEALTH_CHECK_RESULTS_TABLE_SQL)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                result_query,
                (
                    result_id,
                    endpoint_id,
                    checked_at,
                    status_code,
                    latency_ms,
                    success,
                    error,
                ),
            )
            cursor.execute(
                endpoint_query,
                (current_state, checked_at, next_check_at, endpoint_id),
            )
            endpoint = cursor.fetchone()

    if endpoint is None:
        raise LookupError(f"endpoint not found: {endpoint_id}")
    return endpoint


def delete_endpoint(endpoint_id: object) -> bool:
    """Delete an endpoint, returning whether a record was removed."""
    with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
        connection.execute(ENDPOINTS_TABLE_SQL)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM endpoints WHERE id = %s", (endpoint_id,))
            return cursor.rowcount == 1


def is_database_ready() -> bool:
    """Check whether PostgreSQL can accept and execute a simple query."""
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
    except (OSError, ValueError, psycopg.Error):
        return False
    return True
