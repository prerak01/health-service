from unittest.mock import MagicMock

import psycopg
from fastapi.testclient import TestClient

from health_service.database import DATABASE_URL, is_database_ready
from health_service.main import create_app


def test_health_reports_a_normal_run() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "test_run": False}


def test_health_reports_a_test_run() -> None:
    client = TestClient(create_app(test_run=True))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "test_run": True}


def test_ready_reports_database_connectivity(monkeypatch) -> None:
    monkeypatch.setattr("health_service.main.is_database_ready", lambda: True)
    client = TestClient(create_app())

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "connected"}


def test_ready_reports_unavailable_database(monkeypatch) -> None:
    monkeypatch.setattr("health_service.main.is_database_ready", lambda: False)
    client = TestClient(create_app())

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "unavailable"}


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
