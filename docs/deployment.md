# Deployment

celery-manager is several small processes that share one database and one
configuration. This page is the checklist; the details live in the pages it
links to.

## Process layout

| Process | Command | Count |
|---|---|---|
| Dashboard and API | `uvicorn your_module:app` (or mounted in your ASGI/WSGI app) | one or more |
| Celery worker | `celery -A your_module worker -E` | as many as you need |
| Beat | `celery -A your_module beat -S celery_manager.beat:Scheduler` | exactly one |
| Events listener | `celery-manager events --celery-app your_module:app --db-url ...` | one |
| Migrate (deploy step) | `celery-manager migrate --db-url ...` | once per release |

Every process must build the same `CeleryManagerConfig` (same `db_url`, same
`db_schema`). Only the dashboard process needs `secret_key`.

A complete Compose file with all of these, PostgreSQL and Redis is in
[`examples/docker/`](https://github.com/Pythondeveloper6/celery-manager/tree/main/examples/docker).

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
