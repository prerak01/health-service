from unittest.mock import MagicMock

import psycopg

from health_service.database import DATABASE_URL, is_database_ready


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
