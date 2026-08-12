from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer

from health_service.database import initialize_schema
from health_service.main import create_app
from health_service.scheduler import HealthCheckScheduler


class HealthyEndpointHandler(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_GET(self) -> None:
        type(self).requests.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return None


@pytest.fixture
def postgres_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the integration test against an isolated PostgreSQL container."""
    container = PostgresContainer(
        "postgres:16-alpine",
        username="health_service",
        password="health_service",
        dbname="health_service",
    )
    try:
        container.start()
    except Exception as error:
        pytest.skip(f"Docker/PostgreSQL test container is unavailable: {error}")

    try:
        database_url = (
            f"postgresql://{container.username}:{container.password}@"
            f"{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
            f"/{container.dbname}"
        )
        monkeypatch.setenv("DATABASE_URL", database_url)
        initialize_schema()
        yield
    finally:
        container.stop()


@pytest.fixture
def healthy_endpoint_server() -> Iterator[str]:
    """Run a deterministic local HTTP target for the real health checker."""
    HealthyEndpointHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthyEndpointHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://127.0.0.1:{server.server_port}/health"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.integration
def test_check_store_and_query_full_path(
    postgres_database: None,
    healthy_endpoint_server: str,
) -> None:
    client = TestClient(create_app(enable_scheduler=False))
    endpoint_id: UUID | None = None

    try:
        registration = client.post(
            "/endpoints",
            json={
                "url": healthy_endpoint_server,
                "check_interval_seconds": 30,
                "expected_status_code": 200,
            },
        )

        assert registration.status_code == 201
        endpoint = registration.json()
        endpoint_id = UUID(endpoint["id"])
        created_at = datetime.fromisoformat(endpoint["created_at"])

        scheduler = HealthCheckScheduler()
        try:
            scheduler.run_once()
        finally:
            # stop() waits for the worker submitted by run_once() to finish.
            scheduler.stop()

        assert HealthyEndpointHandler.requests == ["/health"]

        history = client.get(
            f"/endpoints/{endpoint_id}/history",
            params={
                "start_time": created_at.isoformat(),
                "end_time": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            },
        )

        assert history.status_code == 200
        records = history.json()
        assert len(records) == 1
        assert records[0]["endpoint_id"] == str(endpoint_id)
        assert records[0]["status_code"] == 200
        assert records[0]["success"] is True
        assert records[0]["error"] is None

        current_endpoint = client.get("/endpoints").json()[0]
        assert current_endpoint["id"] == str(endpoint_id)
        assert current_endpoint["current_state"] == "healthy"
    finally:
        if endpoint_id is not None:
            client.delete(f"/endpoints/{endpoint_id}")
