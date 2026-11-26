# Deployment

celery-manager is several small processes that share one database and one
configuration. This page is the checklist; the details live in the pages it
links to.

## Process layout

| Process | Command | Count |
|---|---|---|
| Dashboard and API | `celery-manager serve --config module:attr` (needs `celery-manager[standalone]`), `uvicorn your_module:app`, or mounted in your ASGI/WSGI app | one or more |
| Celery worker | `celery -A your_module worker -E` | as many as you need |
| Beat | `celery -A your_module beat -S celery_manager.beat:Scheduler` | exactly one |
| Events listener | `celery-manager events --celery-app your_module:app --db-url ...` | one |
| Migrate (deploy step) | `celery-manager migrate --db-url ...` | once per release |

### Events listener health

The listener reconnects to the broker with exponential backoff
(`listener_reconnect_max_seconds`), skips an event it cannot store (logging
only its type and task id) and exits after `listener_max_consecutive_errors`
failures in a row. `SIGTERM` stops capture, writes a last heartbeat and exits 0.

Every 15 seconds it writes a heartbeat row (instance, last event time,
events processed and failed, reconnects, lag). `GET /api/health/listener`,
the `listener_stale` alert and the `celery_manager_listener_*` metrics read it.
A listener that is connected but receives nothing goes stale after two
minutes; on a cluster with no workers that means "stale" is expected.

For a container probe that needs no credentials:

- `celery-manager events --health-file /tmp/alive` touches the file on every heartbeat while healthy; probe on its age.
- `celery-manager events --health-port 8081` answers every GET with an empty 200, or 503 when no event arrived for two minutes. It listens on all interfaces and sends no data.

Run one listener: a standby that takes over on its own is not built in, so
let the platform restart the process.

Every process must build the same `CeleryManagerConfig` (same `db_url`, same
`db_schema`). Only the dashboard process needs `secret_key`.

A complete Compose file with all of these, PostgreSQL and Redis is in
[`examples/docker/`](https://github.com/Pythondeveloper6/celery-manager/tree/main/examples/docker).
To run every process on one machine with a single command (no Docker
Compose, just a Redis), see
[`examples/full_stack/`](https://github.com/Pythondeveloper6/celery-manager/tree/main/examples/full_stack).

### Mounted in a synchronous WSGI host

Each Workers or Queues refresh asks the broker and holds a request worker
while it waits. Set `inspect_source = "snapshot"`: the `celery-manager events`
process then stores the workers' replies and the queue listing every
`inspect_snapshot_seconds` (default 10), the pages read only the database and
show the snapshot's time, and no dashboard `GET` opens a broker connection.
Actions (shutdown, pool resize, purge) still go to the broker. The other
supported layout is a standalone `celery-manager serve` behind the same host
name, signed in through the OIDC flow.

`GET /livez` answers 200 without touching anything; `GET /readyz` answers 200
when the database replies within one second and 503 otherwise. Neither needs
credentials.

## Checklist

1. **Database.** Use PostgreSQL or MySQL for anything beyond a single machine.
   SQLite is for development and the demo.
2. **Migrations.** Run `celery-manager migrate` as a deploy step and start the
   long-running processes with `auto_migrate=False`. See
   [Schema and upgrades](configuration.md#schema-and-upgrades).
3. **Secrets.** Set a long random `secret_key` from your secret store. See
   [`secret_key` in production](configuration.md#secret_key-in-production).
4. **HTTPS.** Serve the dashboard over HTTPS and keep `cookie_secure=True`.
5. **Accounts.** There is no default account: run
   `celery-manager create-user admin --role admin --db-url ...`.
6. **Celery settings.** Enable task events (`worker_send_task_events`,
   `task_send_sent_event`). `celery-manager doctor` is a read-only preflight
   that reports what is missing.
7. **One beat.** Run exactly one beat with the celery-manager scheduler.
8. **Retention.** Set retention rules and run `celery-manager prune` on a
   schedule; see [Data retention](retention.md).
9. **Network.** Keep the dashboard on a private network or behind your
   identity-aware proxy; see [Security](security.md).

## Health and monitoring

Run `celery-manager doctor` after each deploy. For metrics and alerting, see
[Metrics](metrics.md), [Alerts](alerts.md) and [Beat liveness](beat-liveness.md).
Production specifics (external schedulers, OIDC, storage isolation) are in
[Production adoption](production.md).
