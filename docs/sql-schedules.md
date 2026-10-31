# Durable SQL schedules and explicit ownership

Install `celery-manager[sqlalchemy-beat]` to store schedule definitions in
sqlalchemy-celery-beat's SQL tables. This is separate from celery-manager's
monitoring tables and separate from Celery's result backend. Importing
celery-manager does not require the optional scheduler package.

## Deployment

Use the same URL and schema in the dashboard process and the beat process:

```python
from celery_manager import CeleryManagerConfig
from celery_manager.schedulers import SQLAlchemyBeatBackend

app.conf.beat_dburi = "postgresql+psycopg://user:password@db/tasks"
app.conf.beat_schema = "celery_schedules"  # optional; provision it first
backend = SQLAlchemyBeatBackend(app)
# Run this once as a deployment step, with DDL privileges:
backend.initialize()
config = CeleryManagerConfig(
    celery_app=app,
    db_url="postgresql+psycopg://user:password@db/monitoring",
    secret_key="load-a-secret-from-your-environment",
    scheduler_backend=backend,
)
```

Start one beat process for this schedule namespace:

```shell
celery -A myproject.celery_app beat -S celery_manager.beat:SQLAlchemyDatabaseScheduler
```

The shipped scheduler stamps scheduled messages with their schedule and occurrence
identity, so occurrence pauses and monitoring use the same identity as other
celery-manager schedulers. It retains upstream reserve/sync behavior and persists
run counters. Initialization is explicit: beat does not silently create tables.
Upstream table upgrades remain the deployment operator's responsibility when
upgrading the optional package; celery-manager migrations manage monitoring tables.

Each backend and scheduler instance has an independent engine and schema mapping.
Different projects can use different URLs or schemas in one process. Projects
sharing a URL and schema intentionally share a namespace; schedule names must then
be unique. Named schemas must already exist. SQLite uses no schema by default.
`backend.close()` releases its pool when the host shuts down. Do not run several
beat owners against one namespace without external leader election.

CRUD supports intervals in whole seconds (1–2147483647; the upstream column is
a portable 32-bit integer), crontab (including its timezone), and solar schedules.
Arguments, keyword arguments, enabled state, queue, exchange, routing key,
priority, headers and expiry are persisted. Relative expiry is nonnegative whole
seconds; absolute expiry accepts a timezone-aware datetime or ISO timestamp. Unsupported delivery
options fail rather than being dropped. Existing upstream rows using `one_off`
or `start_time` fail with an explicit error: this adapter cannot yet export or edit
those semantics safely. Clocked schedules and intervals with `relative=True` rounding are not supported. Solar schedules also need Celery's solar
dependencies. The HTTP schedule editor currently accepts interval/crontab specs;
solar definitions can be supplied through Python or an exported document.

## Choose which side owns definitions

SQL is authoritative when using the shipped SQL scheduler. It deliberately does
not import `app.conf.beat_schedule` at every startup: doing so would overwrite
operator edits after a restart. Existing code definitions enter SQL through an
explicit adoption plan. Keep a single owner policy per deployment:

- `backend`: observe differences only. SQL, Redis, or Django Beat remains the
  source of truth and the plan applies no changes.
- `code`: compare code definitions with a writable backend and explicitly adopt
  missing definitions. Existing changes require `allow_updates=True`.
- `sql`: use the same explicit adoption/repair workflow, requiring the SQL backend.
  After adoption, SQL edits survive deployment and beat restart.

This ownership choice controls each reconciliation operation; it is not a second
background sync process. Existing Redis/Django scheduler startup behavior is
unchanged. If a backend's own scheduler imports code definitions at startup,
configure that scheduler consistently with your chosen policy.

## Dry run, adoption and repair from Python

```python
from celery_manager.schedulers.base import StaticBeatBackend
from celery_manager.schedulers.reconcile import (
    export_backend, load_export, plan_reconcile, apply_reconcile,
)

# Dry run: no writes, default backend ownership means observation only.
comparison = plan_reconcile(backend, StaticBeatBackend(app).list())

# Review and persist this JSON plan before applying it.
plan = plan_reconcile(backend, StaticBeatBackend(app).list(), ownership="sql")
result = apply_reconcile(backend, plan, app=app)

# Back up all backend definitions as JSON-serializable data.
backup = export_backend(backend)

# Repair missing entries from a reviewed backup. Existing differences remain
# unapplied until explicitly opting into updates.
desired = load_export(backup, app)
repair = plan_reconcile(backend, desired, ownership="sql", allow_updates=True)
result = apply_reconcile(backend, repair, app=app)
```

A plan lists `create`, `update`, and `extra` changes with complete before/after
values and an `apply` flag. No reconciliation operation deletes schedules;
`extra` always remains unapplied. Individual schedule deletion is a separate,
explicit CRUD operation. A backup contains definitions, not last-run counters,
execution history, monitoring overrides or broker messages. Restoring it does not
replay past tasks. Unsupported schedule objects or non-JSON arguments cause export
to fail, rather than returning a misleading backup.

Apply validates all definitions before its first write, rejects duplicate plan
names, checks a fingerprint of the reviewed backend, then rechecks each target
before changing it. Runtime last-run counters do not invalidate a plan. A definition
change after review raises `ReconcileConflict`; generate and review a fresh plan.
Reconciliation across the supported backends is not one database transaction:
a connection failure or concurrent writer can leave partial progress. Review a
fresh diff after failure. Pause other administrative writers for a consistent
repair; the snapshot check is not a distributed lock.

## Moving from RedBeat or code to SQL

`celery-manager copy-schedules` copies every definition from one backend into
another in one reviewed step. It prints the plan as JSON and writes nothing
unless you pass `--apply`:

```shell
# Review: what would be created in the SQL scheduler from RedBeat?
celery-manager copy-schedules --config myproject.cm:config --from redbeat --to sql
# Apply the same plan.
celery-manager copy-schedules --config myproject.cm:config --from redbeat --to sql --apply
```

`--from` is `code` (the app's `beat_schedule`), `redbeat` or `sql`; `--to` is
`sql` or `redbeat`. The SQL side uses the app's `beat_dburi`/`beat_schema`
unless `--beat-db-url`/`--beat-schema` are given. Entries present only in the
target are reported as `extra` and left alone; entries whose definition differs
are only overwritten with `--allow-updates`. Run-state (last run time, total run
count) is not copied, so each schedule starts fresh in the target.

A safe cut-over: stop beat, run the dry run and review it, apply, switch beat to
`celery_manager.beat:SQLAlchemyDatabaseScheduler`, start
beat, and keep the old RedBeat keys until the new scheduler has fired each
schedule once. The same function is available from Python as
`celery_manager.schedulers.reconcile.copy_schedules(source, target, app=app,
apply=False)`.

Monitoring-side pauses and per-occurrence overrides live in celery-manager's
own tables, not in the scheduler backend, so they survive the move unchanged.
Pauses kept in a legacy table of your own can be imported with
[`celery-manager import-pauses`](release-0.3.md#legacy-pause-csv-import).

## HTTP workflow

Authenticated viewers can download `GET /api/schedule-reconciliation/export`.
Administrators can generate a plan with
`POST /api/schedule-reconciliation/plan`:

```json
{"ownership":"sql","allow_updates":false}
```

Omit `document` to compare code's `beat_schedule`. To restore a backup, pass its
complete export object as `document`. Send the reviewed plan object itself to
`POST /api/schedule-reconciliation/apply`. The response lists `applied` names and
an always-empty `deleted` list. An audit intent is persisted before backend changes, so an unavailable audit
database prevents application. Successful applications also write an outcome and
refresh the dashboard's backend snapshot. The API validates task registration and
reserved occurrence headers before applying. A stale plan returns 409, invalid
definitions return 422. These endpoints obey the same authentication and CSRF
rules as other administrative endpoints. Equivalent `/api/v1` aliases are included by the shipped application.

## Verification limits

The adapter's integration tests exercise the real optional package with SQLite,
including restart durability and the shipped scheduler's stamp/counter contract.
Connection drivers and deployment permissions are supplied by the host. Run the
same deployment smoke test against your PostgreSQL, MySQL or SQL Server version
before enabling production writes; SQLite verification alone does not establish
that every server and isolation configuration behaves identically.

## Dashboard adoption and repair

Administrators can open **Schedules → Schedule backup and reconciliation**.

1. Choose **Export backend JSON** to download the current definitions as a
   version-1 backup. Exports may contain arguments and headers; store them as
   carefully as your scheduler configuration.
2. Leave **Desired export JSON** blank to compare code's current `beat_schedule`,
   or paste a complete exported document to adopt or restore those definitions.
3. Choose **Backend** for observation only, **Code** to adopt desired definitions,
   or **SQL** to adopt into the `sqlalchemy-beat` backend. These are explicit
   reconciliation modes for this operation; applying a plan does not install a
   continuous code-to-backend synchronization job.
4. Enable **Include repairs of differing existing entries** only when existing
   definitions should be updated. New missing definitions can be adopted without
   enabling repairs. Extra backend entries are displayed and never deleted.
5. Choose **Generate review plan**. Expand each difference to inspect its complete
   before/after definition, including task, timezone, arguments and options. A
   selected write can be unchecked; regenerate the plan to select it again.
6. Check the explicit approval statement, then choose **Apply reviewed changes**.
   This is unavailable on a read-only backend or an observation-only plan. Any
   change to inputs invalidates the old plan. Each apply attempt consumes the
   displayed plan and approval, including failures: inspect the backend and
   generate a fresh plan before retrying.

A backend definition changed after review yields a conflict, so the operator must
review again. A network or backend error can leave some writes applied. Export
and inspect the current backend before planning recovery; apply is not a global
transaction and never replays missed executions. Viewers can export via the HTTP
endpoint; the dashboard's management section is restricted to administrators.
