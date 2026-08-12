# Data Retention Strategy

## Policy

This is the proposed production policy from the scaling design. The current
MVP does not enforce it; PostgreSQL data grows until an endpoint is deleted.

| Data | Retention | Storage |
| --- | --- | --- |
| Endpoint configuration, occurrences, outbox, and current state | Until endpoint deletion | Regional HA PostgreSQL |
| Raw health-check results | 30 days | Replicated ClickHouse, daily-partitioned table ordered by endpoint and check time |
| State transitions | 13 months | Regional HA PostgreSQL |
| Hourly availability aggregates | 13 months | Replicated ClickHouse |
| Archived raw results | One year from `checked_at` | Compressed Parquet in S3-compatible object storage |

Retention is based on event time in UTC (`checked_at` or `changed_at`), not
ingestion time. The existing `ON DELETE CASCADE` behavior permanently removes
an endpoint's results and transitions when that endpoint is deleted.

Here, “hot” means queryable directly by the service without restoring an
archive. In production, the scheduler records occurrences and an outbox in
PostgreSQL; workers consume check jobs from Kafka and publish results to a
Kafka result topic. A state consumer updates PostgreSQL, while a history
ingester writes raw results and hourly aggregates to ClickHouse. In the MVP,
all active data remains in PostgreSQL.

## Production enforcement

The Kafka result topic is the handoff point for both consumers. The history
ingester writes daily-partitioned raw results to ClickHouse and maintains the
hourly rollup. A separate archival consumer or batch export reads the same
result stream, writes compressed Parquet to S3, and records an archive
manifest before the corresponding ClickHouse data expires.

Apply the policy with storage-native retention: ClickHouse table TTLs remove
raw results after 30 days, transition data remains in PostgreSQL for 13 months,
and hourly aggregates remain in ClickHouse for 13 months. S3 lifecycle rules
expire archived objects one year after their `checked_at` coverage. Kafka
topics retain enough data for consumer replay and are monitored for lag; Kafka
is not the long-term archive.

Archive publication must be idempotent and verifiable by row count, time
bounds, and checksums. If publication or verification fails, keep the hot data
and retry. Do not let a ClickHouse TTL remove data before the archive manifest
is complete; use a safety margin or a verified-export watermark. This avoids
row-by-row application deletes and keeps cleanup out of the request path.

Archived data is outside the normal history API and should be restored through
an operator-controlled workflow. If archive queries become a product feature,
provide an asynchronous export rather than making normal API requests depend
on object storage.

## Safeguards and failure handling

- On export or verification failure, retain the hot partition and retry; never
  expire unverified data.
- Keep archive manifests longer than their objects and protect object storage
  with encryption, restricted credentials, and reviewed lifecycle rules.
- Define backup and point-in-time-recovery expiry separately; expired data may
  remain recoverable until the backup window closes.
- Kafka retains enough messages for consumers to recover from ClickHouse or S3
  outages; alert before the retention window is exhausted.
- If archival is delayed, continue ingestion and expand storage before
  capacity is exhausted. Manual row deletion is a last resort.

## Monitoring

Emit logs and metrics for archive-consumer lag, Kafka consumer lag, oldest
retained event, data archived/expired/retried, rows and bytes processed, job
duration, consecutive failures, outbox age, and PostgreSQL/ClickHouse disk
utilization. Alert when two scheduled runs fail, consumer lag approaches the
Kafka retention window, archival has no successful publication for two
intervals, verification fails, or projected free space is insufficient for
recovery.

## MVP boundary and tradeoff

The MVP has no partitions, hourly aggregates, archives, or cleanup job; its
history endpoints return all matching rows still in PostgreSQL. Thirty days of
raw data prioritizes recent outage investigation, while 13 months of
transitions and aggregates supports annual comparisons at lower volume. A
one-year archive provides economical exceptional-history access but requires a
separate restore workflow. Implementing this policy requires partition
migrations, archival, aggregation, object storage, metrics, and restore tests.
