# celery-manager

A dashboard and control API for Celery background tasks: see every task's
status and logs, start/stop/cancel/retry, and pause a single future
occurrence of a periodic task without touching its schedule.

- [Quickstart](quickstart.md)
- [The `@track` decorator](decorator.md)
- [REST API reference](rest-api.md)
- [Dashboard walkthrough](dashboard.md)
- [Configuration reference](configuration.md)

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
3. **The `@track` decorator** — optional, per-task. It adds full log capture
   and lets the dashboard pause/cancel one specific future occurrence of a
   periodic task.

All three read and write the same SQLAlchemy-backed store (`db_url` in
[`CeleryManagerConfig`](configuration.md)), so any combination of them can
run in separate processes or containers.
