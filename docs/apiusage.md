# API usage

After the ClusterIP Service is forwarded to local port `8000`, use
`http://127.0.0.1:8000` as the base URL. IDs and timestamps in responses will
vary.

## Health

`GET /health` reports whether the service process is running.

```bash
curl http://127.0.0.1:8000/health
```

`200 OK`

```json
{"status":"ok"}
```

## Readiness

`GET /ready` reports whether the service can connect to PostgreSQL.

```bash
curl http://127.0.0.1:8000/ready
```

`200 OK`

```json
{"status":"ready","database":"connected"}
```

## Metrics

`GET /metrics/` returns Prometheus-compatible metrics.

```bash
curl http://127.0.0.1:8000/metrics/
```

`200 OK`

```text
# HELP health_service_health_checks_total Health checks grouped by whether the endpoint returned an HTTP status code.
# TYPE health_service_health_checks_total counter
# HELP health_service_scheduler_tasks_pending Tasks submitted to the scheduler executor that have not started yet.
# TYPE health_service_scheduler_tasks_pending gauge
health_service_scheduler_tasks_pending 0.0
```

## Register an endpoint

`POST /endpoints` registers an endpoint to monitor.

```bash
curl --request POST http://127.0.0.1:8000/endpoints \
  --header 'content-type: application/json' \
  --data '{"url":"http://health-service:8000/health","check_interval_seconds":5,"expected_status_code":200}'
```

`201 Created`

```json
{
  "id": "9e87a45e-8392-4248-ba76-2fa20cd3bed6",
  "url": "http://health-service:8000/health",
  "check_interval_seconds": 5,
  "expected_status_code": 200,
  "current_state": "pending",
  "last_checked_at": null,
  "next_check_at": null,
  "created_at": "2026-08-11T21:42:23.497889Z"
}
```

## List endpoints

`GET /endpoints` lists all registered endpoints and their current state.

```bash
curl http://127.0.0.1:8000/endpoints
```

`200 OK`

```json
[
  {
    "id": "9e87a45e-8392-4248-ba76-2fa20cd3bed6",
    "url": "http://health-service:8000/health",
    "check_interval_seconds": 5,
    "expected_status_code": 200,
    "current_state": "healthy",
    "last_checked_at": "2026-08-11T21:42:49.667037Z",
    "next_check_at": "2026-08-11T21:42:54.667037Z",
    "created_at": "2026-08-11T21:42:23.497889Z"
  }
]
```

## Get health-check history

`GET /endpoints/{endpoint_id}/history` returns checks in an inclusive time
range.

```bash
curl "http://127.0.0.1:8000/endpoints/9e87a45e-8392-4248-ba76-2fa20cd3bed6/history?start_time=2026-08-11T21:43:00Z&end_time=2026-08-11T21:43:10Z"
```

`200 OK`

```json
[
  {
    "id": "da103dd0-1197-405c-a6c4-cbd0738126a9",
    "endpoint_id": "9e87a45e-8392-4248-ba76-2fa20cd3bed6",
    "checked_at": "2026-08-11T21:43:09.778230Z",
    "status_code": 200,
    "latency_ms": 3,
    "success": true,
    "error": null
  }
]
```

## Get state transitions

`GET /endpoints/{endpoint_id}/transitions` returns state changes in an
inclusive time range.

```bash
curl "http://127.0.0.1:8000/endpoints/9e87a45e-8392-4248-ba76-2fa20cd3bed6/transitions?start_time=2026-08-11T21:42:24Z&end_time=2026-08-11T21:42:30Z"
```

`200 OK`

```json
[
  {
    "id": "7f9df9f5-bb42-41cd-8f17-a9529edf4b77",
    "endpoint_id": "9e87a45e-8392-4248-ba76-2fa20cd3bed6",
    "changed_at": "2026-08-11T21:42:25.873578Z",
    "from_state": "pending",
    "to_state": "healthy"
  }
]
```

## Remove an endpoint

`DELETE /endpoints/{endpoint_id}` removes a registered endpoint.

```bash
curl --request DELETE http://127.0.0.1:8000/endpoints/9e87a45e-8392-4248-ba76-2fa20cd3bed6
```

`204 No Content` with an empty response body.
