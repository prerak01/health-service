from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID

import psycopg
from fastapi.testclient import TestClient

from health_service.main import create_app


def test_health_reports_a_normal_run() -> None:
    client = TestClient(create_app(enable_scheduler=False))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "test_run": False}


def test_health_reports_a_test_run() -> None:
    client = TestClient(create_app(test_run=True, enable_scheduler=False))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "test_run": True}


def test_ready_reports_database_connectivity(monkeypatch) -> None:
    monkeypatch.setattr("health_service.main.is_database_ready", lambda: True)
    client = TestClient(create_app(enable_scheduler=False))

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "connected"}


def test_ready_reports_unavailable_database(monkeypatch) -> None:
    monkeypatch.setattr("health_service.main.is_database_ready", lambda: False)
    client = TestClient(create_app(enable_scheduler=False))

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "unavailable"}


def test_create_endpoint_persists_and_returns_the_stored_record(monkeypatch) -> None:
    endpoint_id = UUID("e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8")
    created_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    persisted = {
        "id": endpoint_id,
        "url": "https://example.com/health",
        "check_interval_seconds": 30,
        "expected_status_code": 200,
        "current_state": "pending",
        "last_checked_at": None,
        "next_check_at": None,
        "created_at": created_at,
    }
    persist = MagicMock(return_value=persisted)
    monkeypatch.setattr("health_service.main.persist_endpoint", persist)
    client = TestClient(create_app(enable_scheduler=False))

    response = client.post(
        "/endpoints",
        json={
            "url": "https://example.com/health",
            "check_interval_seconds": 30,
            "expected_status_code": 200,
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "id": str(endpoint_id),
        "url": "https://example.com/health",
        "check_interval_seconds": 30,
        "expected_status_code": 200,
        "current_state": "pending",
        "last_checked_at": None,
        "next_check_at": None,
        "created_at": "2026-08-10T12:00:00Z",
    }
    assert persist.call_args.kwargs["endpoint_id"]
    assert persist.call_args.kwargs["created_at"].tzinfo is UTC


def test_create_endpoint_validates_its_configuration() -> None:
    client = TestClient(create_app(enable_scheduler=False))

    response = client.post(
        "/endpoints",
        json={
            "url": "ftp://example.com",
            "check_interval_seconds": 0,
            "expected_status_code": 600,
        },
    )

    assert response.status_code == 422


def test_list_endpoints_returns_registered_records(monkeypatch) -> None:
    endpoints = [
        {
            "id": UUID("e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8"),
            "url": "https://example.com/health",
            "check_interval_seconds": 30,
            "expected_status_code": 200,
            "current_state": "pending",
            "last_checked_at": None,
            "next_check_at": None,
            "created_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        }
    ]
    monkeypatch.setattr("health_service.main.fetch_endpoints", lambda: endpoints)
    client = TestClient(create_app(enable_scheduler=False))

    response = client.get("/endpoints")

    assert response.status_code == 200
    assert response.json()[0]["id"] == str(endpoints[0]["id"])
    assert response.json()[0]["current_state"] == "pending"


def test_list_endpoints_returns_an_empty_list(monkeypatch) -> None:
    monkeypatch.setattr("health_service.main.fetch_endpoints", lambda: [])
    client = TestClient(create_app(enable_scheduler=False))

    response = client.get("/endpoints")

    assert response.status_code == 200
    assert response.json() == []


def test_delete_endpoint_returns_no_content(monkeypatch) -> None:
    remove = MagicMock(return_value=True)
    monkeypatch.setattr("health_service.main.remove_endpoint", remove)
    client = TestClient(create_app(enable_scheduler=False))
    endpoint_id = "e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8"

    response = client.delete(f"/endpoints/{endpoint_id}")

    assert response.status_code == 204
    assert response.content == b""
    remove.assert_called_once_with(UUID(endpoint_id))


def test_delete_endpoint_returns_not_found_for_a_missing_id(monkeypatch) -> None:
    monkeypatch.setattr("health_service.main.remove_endpoint", lambda _: False)
    client = TestClient(create_app(enable_scheduler=False))

    response = client.delete("/endpoints/e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8")

    assert response.status_code == 404


def test_endpoint_routes_report_database_unavailability(monkeypatch) -> None:
    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("database is unavailable")

    monkeypatch.setattr("health_service.main.fetch_endpoints", unavailable)
    client = TestClient(create_app(enable_scheduler=False))

    response = client.get("/endpoints")

    assert response.status_code == 503
