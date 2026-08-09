from fastapi.testclient import TestClient

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
