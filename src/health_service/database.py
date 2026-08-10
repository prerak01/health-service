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

ENDPOINT_STATE_TRANSITIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS endpoint_state_transitions (
    id UUID PRIMARY KEY,
    endpoint_id UUID NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    changed_at TIMESTAMPTZ NOT NULL,
    from_state TEXT NULL
        CHECK (from_state IS NULL OR from_state IN ('pending', 'healthy', 'unhealthy')),
    to_state TEXT NOT NULL
        CHECK (to_state IN ('pending', 'healthy', 'unhealthy')),
    CHECK (from_state IS NULL OR from_state <> to_state)
)
"""

ENDPOINT_STATE_TRANSITIONS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS endpoint_state_transitions_endpoint_changed_at_idx
ON endpoint_state_transitions (endpoint_id, changed_at DESC)
"""

ENDPOINT_FIELDS = """
id, url, check_interval_seconds, expected_status_code, current_state,
last_checked_at, next_check_at, created_at
"""

HEALTH_CHECK_RESULT_FIELDS = """
id, endpoint_id, checked_at, status_code, latency_ms, success, error
"""

STATE_TRANSITION_FIELDS = """
id, endpoint_id, changed_at, from_state, to_state
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
        connection.execute(ENDPOINT_STATE_TRANSITIONS_TABLE_SQL)
        connection.execute(ENDPOINT_STATE_TRANSITIONS_INDEX_SQL)


def create_endpoint(
    *,
    endpoint_id: object,
    transition_id: object,
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
        connection.execute(ENDPOINT_STATE_TRANSITIONS_TABLE_SQL)
        connection.execute(ENDPOINT_STATE_TRANSITIONS_INDEX_SQL)
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
            if endpoint is None:
                raise LookupError(f"endpoint was not created: {endpoint_id}")
            cursor.execute(
                """
                INSERT INTO endpoint_state_transitions
                    (id, endpoint_id, changed_at, from_state, to_state)
                VALUES (%s, %s, %s, NULL, 'pending')
                """,
                (transition_id, endpoint_id, created_at),
            )
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
        connection.execute(ENDPOINT_STATE_TRANSITIONS_TABLE_SQL)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT current_state FROM endpoints WHERE id = %s FOR UPDATE",
                (endpoint_id,),
            )
            endpoint_state = cursor.fetchone()
            if endpoint_state is None:
                raise LookupError(f"endpoint not found: {endpoint_id}")
            previous_state = endpoint_state.get("current_state")

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
            if previous_state != current_state:
                cursor.execute(
                    """
                    INSERT INTO endpoint_state_transitions
                        (id, endpoint_id, changed_at, from_state, to_state)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        result_id,
                        endpoint_id,
                        checked_at,
                        previous_state,
                        current_state,
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


def list_health_check_history(
    endpoint_id: object,
    *,
    start_time: datetime,
    end_time: datetime,
) -> list[dict[str, object]]:
    """Return an endpoint's health checks within an inclusive time range."""
    query = f"""
    SELECT {HEALTH_CHECK_RESULT_FIELDS}
    FROM health_check_results
    WHERE endpoint_id = %s
      AND checked_at >= %s
      AND checked_at <= %s
    ORDER BY checked_at DESC, id DESC
    """
    with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
        connection.execute(ENDPOINTS_TABLE_SQL)
        connection.execute(HEALTH_CHECK_RESULTS_TABLE_SQL)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT 1 FROM endpoints WHERE id = %s", (endpoint_id,))
            if cursor.fetchone() is None:
                raise LookupError(f"endpoint not found: {endpoint_id}")
            cursor.execute(query, (endpoint_id, start_time, end_time))
            return list(cursor.fetchall())


def list_state_transitions(
    endpoint_id: object,
    *,
    start_time: datetime,
    end_time: datetime,
) -> list[dict[str, object]]:
    """Return an endpoint's state transitions within an inclusive time range."""
    query = f"""
    SELECT {STATE_TRANSITION_FIELDS}
    FROM endpoint_state_transitions
    WHERE endpoint_id = %s
      AND changed_at >= %s
      AND changed_at <= %s
    ORDER BY changed_at DESC, id DESC
    """
    with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
        connection.execute(ENDPOINTS_TABLE_SQL)
        connection.execute(ENDPOINT_STATE_TRANSITIONS_TABLE_SQL)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT 1 FROM endpoints WHERE id = %s", (endpoint_id,))
            if cursor.fetchone() is None:
                raise LookupError(f"endpoint not found: {endpoint_id}")
            cursor.execute(query, (endpoint_id, start_time, end_time))
            return list(cursor.fetchall())


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
