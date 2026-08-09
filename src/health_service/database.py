"""PostgreSQL connectivity helpers for service readiness."""

from __future__ import annotations

import psycopg


DATABASE_URL = (
    "postgresql://health_service:health_service@127.0.0.1:5432/health_service"
)


def is_database_ready() -> bool:
    """Check whether PostgreSQL can accept and execute a simple query."""
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
    except (OSError, ValueError, psycopg.Error):
        return False
    return True
