# Beat owners and external dead-man monitoring

All shipped stamping schedulers record liveness from `tick()`, including ticks
that do not send a task. Monitoring a task's success alone cannot detect a beat
process that stops when no tasks are due.

## Durable owner heartbeats

Configure the beat process with the monitoring database, not the SQL scheduler's
definition database:

```python
app.conf.update(
    celery_manager_db_url="postgresql+psycopg://user:password@db/monitoring",
    celery_manager_db_schema="monitoring",  # optional
    celery_manager_heartbeat_interval=30,
    cm_beat_owner_id="orders-production",  # stable, unique logical owner
)
```

Deploy celery-manager's monitoring migrations before starting beat. Heartbeat
recording does not create tables or run migrations. The interval is bounded to
5–86400 seconds, with a default of 30. Beat's sleep is capped by this interval so
an otherwise idle scheduler can still report. A database write failure is logged
and does not stop task scheduling.

Choose a stable owner ID for each independently expected scheduler; reuse it
across restarts and give separate codebases distinct IDs. Without an explicit
ID, the default combines hostname, Celery application name and scheduler class.
That default may change when container hostnames change and cannot distinguish
two schedulers with identical names on the same host. Explicit IDs are the
production recommendation. An owner is a logical identity, not a distributed
lock or leader-election mechanism.

`GET /api/health/beat` retains `state`, `last_tick`, `age_seconds`, `hostname`,
and `scheduler`, and adds `owner_id` plus an `owners` list. Each owner includes its
own timestamp, age and state. If any recorded owner is stale, overall state is
`stale`, even when another codebase is healthy. The legacy aggregate last-tick
fields still describe the latest legacy heartbeat; consult `owners` to locate a
stale process. Missing or malformed data is `unknown`. Threshold comparison is
strictly greater than `stale_seconds` (default 120).

Use `GET /api/health/beat?owner_id=orders-production&stale_seconds=120` to monitor
one expected owner. An owner that has never reported is unknown, so external
monitoring should also alert on unknown when that owner is expected to exist.
The `beat_stale` alert checks aggregate health and cannot let another owner's
healthy heartbeat hide a recorded stale owner.

Owner rows are durable. Retire an intentionally removed deployment with
`DELETE /api/health/beat/owner?owner_id=orders-production` as an administrator.
Retirement is audited; a still-running process registers again on its next
heartbeat. Never retire a failing owner just to clear an alert. Legacy heartbeat
rows without owner identity remain readable after upgrade.

## External dead-man's switch

An external uptime service can alert even when celery-manager's API, broker, or
monitoring database is unavailable:

```python
app.conf.update(
    cm_beat_healthcheck_url="https://hc-ping.com/YOUR-PRIVATE-CHECK-ID",
    cm_beat_healthcheck_interval=30,
    cm_beat_healthcheck_timeout=3,
)
```

This opt-in GET request works independently of database heartbeat recording.
Use a separate check URL for each expected owner and configure the service's
period/grace to exceed the ping interval plus normal scheduler jitter.

Requests run in a daemon thread; beat's tick does not wait for the monitor. At
most one request per scheduler can be in flight, retries happen only at the
configured interval, and no queue of pending pings accumulates. The interval is
bounded to 5–86400 seconds and the network timeout to 0.1–10 seconds. An operating
system DNS resolver can take longer than the socket timeout; the single-flight
limit still prevents thread accumulation. Daemon threads do not delay process
exit, so shutdown may interrupt an in-flight ping.

Only HTTP/HTTPS URLs without embedded credentials are accepted. Redirects are
refused. URL paths and query parameters may contain monitoring tokens, so neither
the URL nor request exceptions appear in external ping logs. A failed ping logs
a generic warning and never stops scheduling. No successful task execution is
implied by a successful ping: use task alerts alongside scheduler liveness.
