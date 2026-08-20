# celery-manager

A dashboard and control API for Celery background tasks: see every task's
status and logs, start/stop/cancel/retry, and pause a single future
occurrence of a periodic task without touching its schedule.

- [Quickstart](quickstart.md)
- [The `@track` decorator](decorator.md)
- [Live progress](progress.md)
- [REST API reference](rest-api.md)
- [Dashboard walkthrough](dashboard.md)
- [Configuration reference](configuration.md)
- [Working on celery-manager](contributing.md) — tests, docs, and rebuilding
  the dashboard bundle
- [Changelog](https://github.com/pythondeveloper6/celery-manager/blob/main/CHANGELOG.md)
  — including the one breaking API change so far

## What it does today

- **Full run history**, not just what is in flight — `GET /api/tasks` is
  paginated (`limit`/`offset`, up to 500 rows a page) and returns a total, so
  older runs are reachable rather than falling off the end of a fixed list.
- **Search and filtering** by status, exact task name, a start-time range,
  and a free-text `q` that matches substrings of the task name, the task id,
  and the serialized args/kwargs. It is a text search over stored JSON, not a
  JSON query — see the [REST API page](rest-api.md#filtering-search-and-pagination)
  for exactly what it can and cannot express.
- **Live progress from inside a task**, via
  [`report_progress()`](progress.md) — a count, an optional total, and a
  message, throttled so it is safe in a tight loop. Opt-in per task.
- **Start, cancel/terminate, and retry** any task, with retry lineage
  recorded in both directions on the detail view.
- **Per-occurrence schedule pause/cancel** for periodic tasks, without
  editing the schedule itself.
- **An audit log** of every control action, attributed to the user who took
  it.
- **Branding** — wordmark, accent colour, and logo, set once per deployment
  and applied on the login screen too.

Honest about the edges: live updates are near-live (the task list polls every
few seconds; see [the SSE limitation](rest-api.md#known-limitation-sse-does-not-currently-work-across-processes)),
there is no role system yet (any signed-in user can do anything), and there
is no schema-migration framework.

See the [Django demo](https://github.com/pythondeveloper6/celery-manager/tree/main/examples/django_demo)
for a complete working example.

## How it fits together

celery-manager is three cooperating pieces, all pointed at the same
database:

1. **The REST API + dashboard** (`celery_manager.api.app.create_app`) — a
   FastAPI app that serves the JSON API under `/api` and the built dashboard
   SPA at `/`. Mount it into your own ASGI app or run it standalone.
2. **The events listener** (`celery-manager events` CLI command) — a
   separate long-running process that subscribes to Celery's event bus and
   records every task's status transitions, independent of whether the task
   uses `@track`.
3. **The `@track` decorator** — optional, per-task. It adds full log capture,
   lets the dashboard pause/cancel one specific future occurrence of a
   periodic task, and records the run as `started` before the task body runs
   so an in-flight task is visible (and has somewhere to put
   [live progress](progress.md)) even with no listener process running.

All three read and write the same SQLAlchemy-backed store (`db_url` in
[`CeleryManagerConfig`](configuration.md)), so any combination of them can
run in separate processes or containers.
