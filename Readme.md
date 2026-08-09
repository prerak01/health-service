# Health Service

## Prerequisite

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). It
downloads and manages the project-pinned Python 3.13.15 interpreter, so the
service does not depend on the system Python installation.

## Setup

```bash
uv sync --locked
```

`uv.lock` is committed to the repository. The `--locked` option installs only
the exact dependency versions recorded there.

## Run

### Start PostgreSQL

Start PostgreSQL separately with Docker. This command creates a persistent named
volume and exposes the database to the host application on port 5432:

```bash
docker run --detach \
  --name health-service-postgres \
  --publish 5432:5432 \
  --volume health-service-postgres-data:/var/lib/postgresql/data \
  --env POSTGRES_DB=health_service \
  --env POSTGRES_USER=health_service \
  --env POSTGRES_PASSWORD=health_service \
  postgres:16-alpine
```

The service always uses the matching local connection URL:

```text
postgresql://health_service:health_service@127.0.0.1:5432/health_service
```

Stop and later restart the local database without losing data:

```bash
docker stop health-service-postgres
docker start health-service-postgres
```

To discard all local PostgreSQL data, remove the container and its named volume:

```bash
docker rm health-service-postgres
docker volume rm health-service-postgres-data
```

Start a normal service run:

```bash
uv run health-service
```

Start a test run:

```bash
uv run health-service --test-run
```

The service listens on `http://127.0.0.1:8000`. `GET /health` returns:

```json
{"status": "ok", "test_run": false}
```

`GET /ready` verifies that PostgreSQL is reachable. It returns `200` when the
database is connected and `503` when it is unavailable.

## Test

```bash
uv run pytest
```
