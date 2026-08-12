# Runbook: Health Checks Stop Executing

Use this runbook when registered endpoints are overdue but no new check results
are recorded. An individual endpoint becoming unhealthy is not this failure.

The API and scheduler run in the same pod. `/health` only confirms that the API
process is alive; it does not confirm that scheduling works.

## 1. Confirm the incident

The commands assume the default Helm installation:

```bash
export HS_NAMESPACE=health-service
export HS_APP=health-service
export HS_DATABASE=health-service-postgresql

kubectl --namespace "$HS_NAMESPACE" get pods
kubectl --namespace "$HS_NAMESPACE" logs deployment/"$HS_APP" --since=15m
```

The application and PostgreSQL pods should both show `1/1 Running`. `Pending`,
`CrashLoopBackOff`, frequent restarts, or a missing pod indicates a workload
problem. Healthy scheduler logs repeat approximately every five seconds:

```text
health-check scheduler scan completed: due=2 scheduled=2 skipped=0
health check succeeded for endpoint ...
```

`health-check scheduler scan failed`, a traceback, or no scan messages while
endpoints are overdue identifies the scheduler or its database dependency as
the problem.

Forward the application Service in a separate terminal:

```bash
kubectl --namespace "$HS_NAMESPACE" port-forward service/"$HS_APP" 8000:8000
```

Check readiness and metrics:

```bash
curl --silent --show-error --include http://127.0.0.1:8000/ready
curl --fail --silent --show-error http://127.0.0.1:8000/metrics/ \
  | grep -E '^health_service_(health_checks_total|scheduler_tasks_pending)'
```

A healthy readiness response is:

```text
HTTP/1.1 200 OK
{"status":"ready","database":"connected"}
```

`503 Service Unavailable` with `"database":"unavailable"` means PostgreSQL
cannot be reached. Representative metrics look like:

```text
health_service_health_checks_total{outcome="response"} 42.0
health_service_health_checks_total{outcome="no_response"} 3.0
health_service_scheduler_tasks_pending 0.0
```

Read the metrics twice, one expected check cycle apart. One or both counter
values should increase when endpoints are due. A briefly nonzero pending value
is normal; a value that remains high or grows indicates a worker backlog.

Confirm that endpoints are overdue:

```bash
kubectl --namespace "$HS_NAMESPACE" exec "${HS_DATABASE}-0" \
  --container postgresql -- sh -ec '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT count(*) AS registered,
       count(*) FILTER (
           WHERE next_check_at IS NULL OR next_check_at <= now()
       ) AS overdue,
       max(last_checked_at) AS newest_check
FROM endpoints;
"'
```

Representative healthy output is:

```text
 registered | overdue |         newest_check
------------+---------+-------------------------------
          5 |       0 | 2026-08-12 10:30:04.123456+00
```

`registered = 0` means there is no work to execute. `overdue > 0` can occur
briefly around a scheduler scan, but a growing overdue count combined with an
old or null `newest_check` confirms that checks are not being persisted.

The incident is confirmed when endpoints are overdue, `newest_check` is stale,
and `health_service_health_checks_total` does not increase over an expected
check cycle.

## 2. Diagnose and recover

| Evidence | Cause and action |
| --- | --- |
| Application pod is missing, restarting, or OOM-killed | Run `kubectl --namespace "$HS_NAMESPACE" describe deployment/"$HS_APP"` and inspect events. Messages such as `FailedScheduling`, `ErrImagePull`, or `OOMKilled` identify the corrective action. |
| `/ready` returns `503` or scheduler logs show database errors | Run `kubectl --namespace "$HS_NAMESPACE" get pods,pvc` and `kubectl --namespace "$HS_NAMESPACE" logs statefulset/"$HS_DATABASE"`. Restore database connectivity or storage; overdue checks will resume automatically. |
| `/ready` succeeds, endpoints are overdue, and scheduler scan logs have stopped | Capture the logs, then restart the application Deployment. |
| Scheduler scans continue but `health_service_scheduler_tasks_pending` remains high | Check application resources, DNS, and outbound connectivity. Correct the constraint and confirm the queue drains. |

Restart a stalled scheduler with:

```bash
kubectl --namespace "$HS_NAMESPACE" rollout restart deployment/"$HS_APP"
kubectl --namespace "$HS_NAMESPACE" rollout status deployment/"$HS_APP" \
  --timeout=2m
```

A successful restart ends with output similar to:

```text
deployment.apps/health-service restarted
deployment "health-service" successfully rolled out
```

A timeout means the replacement pod did not become ready; return to the pod
status, events, and application logs instead of repeatedly restarting it.

Do not scale the application above one replica because this can execute
duplicate checks. Do not delete endpoints or the PostgreSQL PVC; endpoint
deletion also removes its history and transitions.

## 3. Confirm recovery

Observe at least two expected check cycles and confirm:

- `/ready` returns `200`.
- Scheduler logs show completed scans and check outcomes.
- `health_service_health_checks_total` increases.
- `last_checked_at` advances and the overdue count drains.

Recovery is complete only when all four signals agree. A healthy API response
alone is not sufficient.
