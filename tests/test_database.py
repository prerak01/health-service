from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID

import psycopg

from health_service.database import (
    DATABASE_URL,
    is_database_ready,
    list_due_endpoints,
    record_health_check,
)


def test_database_readiness_executes_a_simple_query(monkeypatch) -> None:
    connection = MagicMock()
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr("health_service.database.psycopg.connect", connect)

    assert is_database_ready() is True
    connect.assert_called_once_with(DATABASE_URL, connect_timeout=3)
    connection.execute.assert_called_once_with("SELECT 1")


def test_database_readiness_handles_connection_failure(monkeypatch) -> None:
    def fail_connection(*args, **kwargs):
        raise psycopg.OperationalError("database is unavailable")

    monkeypatch.setattr("health_service.database.psycopg.connect", fail_connection)

    assert is_database_ready() is False


def test_list_due_endpoints_queries_null_and_past_schedules(monkeypatch) -> None:
    connection = MagicMock()
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = []
    connection.cursor.return_value = cursor
    monkeypatch.setattr("health_service.database.psycopg.connect", connect)
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    assert list_due_endpoints(now) == []

    query, parameters = cursor.execute.call_args.args
    assert "next_check_at IS NULL OR next_check_at <= %s" in query
    assert parameters == (now,)


def test_record_health_check_updates_endpoint_to_healthy(monkeypatch) -> None:
    connection = MagicMock()
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"id": UUID("e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8")}
    connection.cursor.return_value = cursor
    monkeypatch.setattr("health_service.database.psycopg.connect", connect)
    checked_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    endpoint_id = UUID("e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8")

    record_health_check(
        result_id=UUID("4c6c4087-f7c2-4114-93bf-a1bbd5377d8d"),
        endpoint_id=endpoint_id,
        checked_at=checked_at,
        status_code=200,
        latency_ms=25,
        success=True,
        error=None,
        check_interval_seconds=30,
    )

    endpoint_update = cursor.execute.call_args_list[-1].args[1]
    assert endpoint_update == (
        "healthy",
        checked_at,
        datetime(2026, 8, 10, 12, 0, 30, tzinfo=UTC),
        endpoint_id,
    )


def test_record_health_check_updates_endpoint_to_unhealthy(monkeypatch) -> None:
    connection = MagicMock()
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"id": UUID("e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8")}
    connection.cursor.return_value = cursor
    monkeypatch.setattr("health_service.database.psycopg.connect", connect)
    checked_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    endpoint_id = UUID("e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8")

    record_health_check(
        result_id=UUID("4c6c4087-f7c2-4114-93bf-a1bbd5377d8d"),
        endpoint_id=endpoint_id,
        checked_at=checked_at,
        status_code=None,
        latency_ms=2000,
        success=False,
        error="TimeoutError: request timed out",
        check_interval_seconds=30,
    )

    endpoint_update = cursor.execute.call_args_list[-1].args[1]
    assert endpoint_update[0] == "unhealthy"
