# Health Service

## Prerequisites

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) only if
you want to run the Python development workflow or tests locally. It downloads
and manages the project-pinned Python 3.13.15 interpreter.

The local Kubernetes workflow requires:

- [Docker](https://docs.docker.com/engine/install/)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm 3](https://helm.sh/docs/intro/install/)

## Python development setup (optional)

```bash
uv sync --locked
```

`uv.lock` is committed to the repository. The `--locked` option installs only
the exact dependency versions recorded there.

## Local deployment with kind and Helm

The Helm chart is the Kubernetes source of truth; no separate raw manifest set
is maintained. It deploys one application `Deployment`, one PostgreSQL
`StatefulSet`, their internal Services, a Secret, and one persistent volume
claim. PostgreSQL starts automatically with the application, and the service
creates its database tables automatically after startup.

### 1. Build the image, create the cluster, and load the image

```bash
docker build --tag health-service:0.1.0 .
kind create cluster --name health-service
kind load docker-image health-service:0.1.0 --name health-service
```

Loading the locally built image avoids needing a registry. The chart uses
`IfNotPresent`, so Kubernetes uses the image loaded into the kind node.

### 2. Install the chart

```bash
helm upgrade --install health-service charts/health-service \
  --namespace health-service \
  --create-namespace \
  --wait \
  --timeout 3m
```

The default database password is intentionally only a local demo credential.
Override it on the first install when needed:

```bash
helm upgrade --install health-service charts/health-service \
  --namespace health-service \
  --create-namespace \
  --set-string postgresql.auth.password=a-local-password \
  --wait
```

PostgreSQL initialization variables only apply to an empty data directory. Do
not change this value later while retaining the existing PVC unless the
database password is also migrated.

### 3. Inspect and access the service

```bash
kubectl --namespace health-service get pods,services,pvc
kubectl --namespace health-service logs deployment/health-service
kubectl --namespace health-service logs statefulset/health-service-postgresql
helm test health-service --namespace health-service --logs
```

Forward the ClusterIP Service in a dedicated terminal:

```bash
kubectl --namespace health-service \
  port-forward service/health-service 8000:8000
```

Then inspect the operational endpoints:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/metrics/
```

The service exposes Prometheus-compatible metrics at `/metrics`. Custom metrics
include pending scheduler tasks and health checks that received an HTTP response
versus checks that received no response. The Prometheus client also exposes
standard Python process metrics.

### 4. Exercise check, store, and query

Register the application's in-cluster health endpoint so the check is
deterministic and does not depend on Internet access:

```bash
curl --request POST http://127.0.0.1:8000/endpoints \
  --header 'content-type: application/json' \
  --data '{"url":"http://health-service:8000/health","check_interval_seconds":5,"expected_status_code":200}'
```

Copy the returned `id`, wait at least ten seconds, and use it below:

```bash
ENDPOINT_ID=replace-with-returned-id

curl http://127.0.0.1:8000/endpoints
curl --get "http://127.0.0.1:8000/endpoints/${ENDPOINT_ID}/history" \
  --data-urlencode 'start_time=2020-01-01T00:00:00Z' \
  --data-urlencode 'end_time=2100-01-01T00:00:00Z'
```

### 5. Verify persistence

Delete only the PostgreSQL pod and wait for its StatefulSet replacement:

```bash
kubectl --namespace health-service delete pod health-service-postgresql-0
kubectl --namespace health-service rollout status \
  statefulset/health-service-postgresql --timeout=2m
```

After `/ready` returns `200` again, list the endpoints and history. The records
should remain because the replacement pod reuses the same PVC.

### 6. Clean up

```bash
helm uninstall health-service --namespace health-service
kubectl --namespace health-service delete pvc data-health-service-postgresql-0
kind delete cluster --name health-service
```

Helm intentionally leaves StatefulSet claims behind on uninstall. Delete the
PVC explicitly only when its stored history is no longer needed. Deleting the
kind cluster also removes all storage belonging to that cluster.

## Endpoint API

The service creates its endpoint and health-check result tables automatically.
After startup, a scheduler scans for due endpoints every five seconds. Each
endpoint check runs in a worker pool capped at 50 concurrent checks and has a
two-second request timeout.

### Register an endpoint

```bash
curl --request POST http://127.0.0.1:8000/endpoints \
  --header 'content-type: application/json' \
  --data '{"url":"https://example.com/health","check_interval_seconds":30,"expected_status_code":200}'
```

It returns `201 Created` and the persisted endpoint. `id` and lifecycle fields
are service-managed; a new endpoint starts in `pending` state.

```json
{
  "id": "e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8",
  "url": "https://example.com/health",
  "check_interval_seconds": 30,
  "expected_status_code": 200,
  "current_state": "pending",
  "last_checked_at": null,
  "next_check_at": null,
  "created_at": "2026-08-10T12:00:00Z"
}
```

Once a check completes, its result is stored in `health_check_results`. An
endpoint is `healthy` only when the returned HTTP status exactly matches its
configured `expected_status_code`; timeouts, request failures, and mismatched
statuses set it to `unhealthy`. The endpoint's `last_checked_at` and
`next_check_at` fields are updated with the result. The scheduler currently
uses an in-memory ongoing-check set and is intended to run as one service
process. 

### List endpoints

```bash
curl http://127.0.0.1:8000/endpoints
```

It returns `200 OK` and an array of registered endpoint objects in creation
order.

### Get health-check history

```bash
curl "http://127.0.0.1:8000/endpoints/e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8/history?start_time=2026-08-10T00:00:00Z&end_time=2026-08-11T00:00:00Z"
```

Both `start_time` and `end_time` are required timezone-aware ISO-8601
timestamps. The range is inclusive, and results are returned newest first:

```json
[
  {
    "id": "4c6c4087-f7c2-4114-93bf-a1bbd5377d8d",
    "endpoint_id": "e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8",
    "checked_at": "2026-08-10T12:00:00Z",
    "status_code": 200,
    "latency_ms": 25,
    "success": true,
    "error": null
  }
]
```

An unregistered endpoint returns `404 Not Found`.

### Get state transitions

```bash
curl "http://127.0.0.1:8000/endpoints/e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8/transitions?start_time=2026-08-10T00:00:00Z&end_time=2026-08-11T00:00:00Z"
```

The same required inclusive time range is applied to `changed_at`. A newly
registered endpoint records an initial `null` to `pending` event. Later events
are recorded only when the endpoint changes state:

```json
[
  {
    "id": "4c6c4087-f7c2-4114-93bf-a1bbd5377d8d",
    "endpoint_id": "e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8",
    "changed_at": "2026-08-10T12:00:00Z",
    "from_state": "healthy",
    "to_state": "unhealthy"
  }
]
```

An unregistered endpoint returns `404 Not Found`.

### Remove an endpoint

```bash
curl --request DELETE http://127.0.0.1:8000/endpoints/e18e671d-8f3e-4d2c-b3d8-6d540f8e52e8
```

It returns `204 No Content`; an unknown ID returns `404 Not Found`.

## Test

```bash
uv run pytest
```
