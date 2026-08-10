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


def test_history_returns_results_and_normalizes_time_range(monkeypatch) -> None:
    endpoint_id = UUID("e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8")
    result_id = UUID("4c6c4087-f7c2-4114-93bf-a1bbd5377d8d")
    captured: dict[str, object] = {}

    def fetch_history(current_endpoint_id, *, start_time, end_time):
        captured.update(
            endpoint_id=current_endpoint_id,
            start_time=start_time,
            end_time=end_time,
        )
        return [
            {
                "id": result_id,
                "endpoint_id": endpoint_id,
                "checked_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
                "status_code": 200,
                "latency_ms": 25,
                "success": True,
                "error": None,
            }
        ]

    monkeypatch.setattr("health_service.main.fetch_health_check_history", fetch_history)
    client = TestClient(create_app(enable_scheduler=False))

    response = client.get(
        f"/endpoints/{endpoint_id}/history",
        params={
            "start_time": "2026-08-10T17:30:00+05:30",
            "end_time": "2026-08-10T18:30:00+05:30",
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["id"] == str(result_id)
    assert captured == {
        "endpoint_id": endpoint_id,
        "start_time": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        "end_time": datetime(2026, 8, 10, 13, 0, tzinfo=UTC),
    }


def test_transitions_returns_filtered_events(monkeypatch) -> None:
    endpoint_id = UUID("e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8")
    transition_id = UUID("4c6c4087-f7c2-4114-93bf-a1bbd5377d8d")
    fetch_transitions = MagicMock(
        return_value=[
            {
                "id": transition_id,
                "endpoint_id": endpoint_id,
                "changed_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
                "from_state": "pending",
                "to_state": "healthy",
            }
        ]
    )
    monkeypatch.setattr("health_service.main.fetch_state_transitions", fetch_transitions)
    client = TestClient(create_app(enable_scheduler=False))

    response = client.get(
        f"/endpoints/{endpoint_id}/transitions",
        params={
            "start_time": "2026-08-10T12:00:00Z",
            "end_time": "2026-08-10T12:30:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["to_state"] == "healthy"
    fetch_transitions.assert_called_once_with(
        endpoint_id,
        start_time=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 8, 10, 12, 30, tzinfo=UTC),
    )


def test_history_and_transitions_require_valid_time_ranges(monkeypatch) -> None:
    monkeypatch.setattr("health_service.main.fetch_health_check_history", lambda *args, **kwargs: [])
    monkeypatch.setattr("health_service.main.fetch_state_transitions", lambda *args, **kwargs: [])
    client = TestClient(create_app(enable_scheduler=False))
    endpoint_id = "e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8"

    missing = client.get(f"/endpoints/{endpoint_id}/history")
    naive = client.get(
        f"/endpoints/{endpoint_id}/transitions",
        params={"start_time": "2026-08-10T12:00:00", "end_time": "2026-08-10T13:00:00Z"},
    )
    reversed_range = client.get(
        f"/endpoints/{endpoint_id}/history",
        params={"start_time": "2026-08-10T13:00:00Z", "end_time": "2026-08-10T12:00:00Z"},
    )

    assert missing.status_code == 422
    assert naive.status_code == 422
    assert reversed_range.status_code == 422


def test_history_and_transitions_return_not_found_for_unknown_endpoint(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise LookupError("endpoint not found")

    monkeypatch.setattr("health_service.main.fetch_health_check_history", missing)
    monkeypatch.setattr("health_service.main.fetch_state_transitions", missing)
    client = TestClient(create_app(enable_scheduler=False))
    endpoint_id = "e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8"
    params = {"start_time": "2026-08-10T12:00:00Z", "end_time": "2026-08-10T13:00:00Z"}

    history = client.get(f"/endpoints/{endpoint_id}/history", params=params)
    transitions = client.get(f"/endpoints/{endpoint_id}/transitions", params=params)

    assert history.status_code == 404
    assert transitions.status_code == 404
