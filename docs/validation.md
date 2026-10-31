# Performance and validation

## Measuring overhead

`scripts/benchmark.py` (in the source repository) measures the two costs that
decide whether celery-manager keeps up with a cluster:

- **listener**: task events stored per second by one `celery-manager events`
  process. Each task produces three or four events.
- **dashboard**: p50/p95 latency of the API calls the dashboard makes most,
  against a seeded week of history.

```bash
python scripts/benchmark.py                                  # SQLite in a temp dir
python scripts/benchmark.py --db-url postgresql+psycopg://user:pw@db/empty_db
```

Point it at an **empty** database of the kind you will run in production. It
does not contact a broker or workers.

### What drives the numbers

Each event is a short transaction of about three round trips (lock or insert
the run, update it, commit). Listener throughput is therefore set by the
database round-trip time, not by CPU:

| Setup (Apple M2 Pro, 3,000 events) | Round trip | Listener | Task list p50 / p95 |
|---|---|---|---|
| SQLite file, same machine | n/a | ~230 events/s | 21 / 48 ms |
| PostgreSQL 16 in Docker Desktop (slow VM network) | ~11 ms | ~11 events/s | 110 / 191 ms |

On a database with a sub-millisecond round trip, the same transaction count
gives hundreds of events per second per listener. If your measured rate is
below your peak task rate × 3, put the database closer to the listener, check
its round-trip time, and keep retention rules so the run table stays bounded.
The dashboard reads one page at a time (`limit` is capped at 500) and caches
the metrics snapshot; list totals still count matching rows, so retention also
keeps page loads fast.

## Release validation matrix

Every release candidate runs these gates against live services in CI (see
`.github/workflows/ci.yml`) and they were also run locally for this release:

| Gate | Service | Covers |
|---|---|---|
| Storage contract, schedules, upgrade from the previous release, native operations | PostgreSQL 16, MySQL 8.4, SQL Server 2022 | Migrations from an empty database and from an older schema revision with data (upgrade and rollback), schema isolation, schedule storage, operational routes. |
| Concurrency races | PostgreSQL, MySQL, SQL Server | Concurrent event upserts, login backoff, locks, retries and schedule edits under real row locking. |
| Broker integration | Redis 7, RabbitMQ 3 (AMQP and management API) | Queue depth, browsing, purge preview, dead letters, unacked restore, worker control. |
| Other runners | dramatiq, taskiq, arq | Run recording through each adapter. |
| Docker example | Compose stack | Web, worker, beat and listener start and record a run. |
