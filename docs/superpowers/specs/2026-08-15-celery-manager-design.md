# celery-manager — Design Spec

Date: 2026-08-15
Status: Approved (pending user spec review)

## 1. Problem

Celery background tasks run invisibly by default: no built-in way to see what's
running, what failed and why, or to control an individual future occurrence of
a periodic task (e.g. skip just the 8pm run of an every-2-hours task without
touching the schedule itself). `celery-manager` is a pip-installable package
that gives any Django/Flask/FastAPI project using Celery a dashboard and API
for this, with minimal integration effort.

## 2. Goals (v1)

- See all background tasks and their status (queued, started, succeeded,
  failed, retried) in near real time, including tasks not specially instrumented.
- Start, stop/cancel, schedule, and reschedule tasks from the dashboard or API.
- View future occurrences of a periodic task and pause/cancel a **single**
  occurrence without altering the recurring schedule.
- See why a task failed, including captured log output from the run — not
  just the traceback.
- One-click and bulk retry of failed tasks.
- Audit log of manual actions taken through the dashboard/API.
- Built-in authentication, zero external auth dependency required.
- Works as a standalone process or mounted into an existing Django/Flask/FastAPI app.
- Storage defaults to SQLite (zero-config) and is configurable to
  Postgres/MySQL/etc via a standard DB URL.

## 3. Non-goals (deferred)

**Phase 2 (fast follow):** worker health view, queue depth/backlog monitoring,
trend analytics (success rate / duration over time), human-friendly cron
builder UI, per-task/queue rate limiting from the UI.

**Phase 3 (later):** alerting (Slack/email/webhook) on failures/backlog,
multi-app/multi-environment support from one dashboard instance.

These are explicitly out of scope for the v1 implementation plan. Nothing in
the v1 architecture should preclude adding them later.

## 4. Architecture

```
┌─────────────────────────┐
│   User's Celery tasks    │  @celery_manager.track  <- decorator: skip-check + log capture
└───────────┬──────────────┘
            │ events (task-sent/started/succeeded/failed/retried, worker heartbeats)
            ▼
┌─────────────────────────┐        ┌──────────────────────────┐
│  Events Listener process │──────▶│   Storage (SQLAlchemy)    │
│  (subscribes to broker)  │       │  SQLite default /         │
└───────────────────────────┘      │  Postgres/MySQL/etc via   │
                                    │  configurable DB URL      │
┌─────────────────────────┐        └───────────┬──────────────┘
│  Core Python API          │◀───────────────────┘
│  celery_manager.control.* │  (list/start/stop/cancel/
│  celery_manager.tasks.*   │   schedule/reschedule/retry)
└───────────┬────────────────┘
            │
┌───────────▼────────────────┐
│  REST API (ASGI, mountable)│  + SSE stream for live updates
└───────────┬────────────────┘
            │
┌───────────▼────────────────┐
│  Dashboard SPA (static,     │  built-in auth · task list/detail ·
│  bundled in the package)    │  future-schedule view · retry/bulk-retry ·
└──────────────────────────────┘  audit log
```

Three independently usable layers: **core Python API → REST API → dashboard
UI**. Anyone can script directly against layer 1 or hit layer 2 over HTTP
without ever opening the UI.

### 4.1 Decorator (`celery_manager.track`)

Wraps a Celery task. Responsibilities:
- Before execution: checks `schedule_overrides` for a skip/cancel entry
  matching this task's scheduled occurrence; no-ops (skips the run body) if
  found.
- During execution: attaches a logging handler scoped to the task run,
  capturing stdout/stderr and `logging` calls, buffered and flushed to
  `task_runs.log_output` on completion.
- Always re-raises real exceptions after capturing traceback/log output —
  never changes task success/failure semantics.
- Designed as the extension point for future per-task controls (custom retry
  policy, log capture scope, etc.) without further Celery internals work.

Applying the decorator is optional per task. Tasks without it are still
visible (via the events listener) but don't support per-occurrence
pause/cancel or full log capture — only the failure traceback Celery already
provides.

### 4.2 Events Listener

A separate long-running process (`celery-manager events` CLI entry point)
subscribing to Celery's built-in event bus (`task-sent`, `task-started`,
`task-succeeded`, `task-failed`, `task-retried`, worker heartbeats). Upserts
`task_runs` rows as events arrive. Reconnects with backoff on broker
disconnect. This is what gives visibility into every task, decorated or not.

### 4.3 Storage

SQLAlchemy models, SQLite by default (zero-config), configurable to any
SQLAlchemy-supported DB via a connection URL in config. Core v1 tables:

- **`task_runs`** — id, task_name, args/kwargs, status, queue, worker,
  eta, started_at, finished_at, runtime, exception, traceback, log_output,
  retry_count
- **`periodic_schedules`** — mirrors the user's beat schedule entries: name,
  task, schedule spec, args/kwargs, enabled
- **`schedule_overrides`** — schedule_name, target occurrence time, action
  (skip/cancel/reschedule), created_by, created_at — the table the decorator
  checks before running a task body
- **`users`** — username, password_hash (built-in auth)
- **`audit_log`** — user, action, target_type, target_id, details, timestamp
  — written on every mutating core-API call

### 4.4 Core Python API

`celery_manager.control` and `celery_manager.tasks` — list runs/schedules,
start (apply_async passthrough), stop/cancel (via Celery's `app.control`
revoke/terminate for in-flight/queued tasks), pause/cancel/reschedule a
specific future occurrence (writes `schedule_overrides`), retry (single or
bulk, optionally with edited args/kwargs). Every mutating call writes an
`audit_log` row. Requires the host app's Celery `app` instance at setup, used
for task introspection (registered task names/signatures) and control calls.

### 4.5 REST API

Thin ASGI HTTP wrapper (framework-agnostic) over the core API, plus an SSE
endpoint streaming task status changes for live dashboard updates. Mountable
as a sub-app into Django/Flask/FastAPI, or runnable standalone.

### 4.6 Dashboard SPA

Prebuilt static bundle (JS/CSS) shipped inside the pip package — no Node
toolchain needed to install or run it. Served by the same ASGI app as the
REST API. Built-in username/password auth (session or JWT cookie).

v1 screens: task list (filter/search), task detail (args, status, log
output, traceback, retry), periodic schedules list, future-occurrence view
with pause/cancel/reschedule, bulk retry on failed tasks, audit log.

## 5. Data flow

**Normal task run:** task fires → events listener records `task-sent` /
`task-started` → (if decorated) decorator checks for a skip override, runs
the task body capturing logs, records success/failure with log output →
events listener also records the terminal event from Celery directly (used
as the source of truth for status; decorator-captured log output is merged
in) → dashboard/API read from `task_runs`.

**Pausing a single future occurrence:** user selects an upcoming occurrence
in the dashboard → REST API → core API writes a `schedule_overrides` row +
`audit_log` entry → at the scheduled time, Celery Beat still dispatches the
task as normal (schedule itself is untouched) → the decorator, running at
task start, finds the matching override and skips the task body → the run is
recorded as "skipped" rather than executed.

## 6. Error handling

- Events listener: reconnect with backoff on broker disconnect; never crashes
  silently — logs and retries.
- Decorator: log capture wraps but never swallows exceptions; a failure in
  the log-capture machinery itself must not prevent the task body from
  running or its real exception from propagating.
- Race handled by design: a pause set after Beat has already dispatched the
  occurrence is still caught, because the check happens at task start inside
  the worker, not at dispatch time.
- REST API: standard 4xx/5xx with structured error bodies; mutating endpoints
  are idempotent where feasible (e.g. re-cancelling an already-cancelled
  occurrence is a no-op, not an error).

## 7. Testing strategy

- Decorator + core API: unit tests using Celery's `task_always_eager` /
  in-memory broker.
- Events listener + storage: integration tests against a real Redis + worker
  in CI.
- REST API: endpoint tests against the core API with a temp SQLite DB.
- UI: deferred to the implementation plan — likely a thin e2e smoke pass, not
  full coverage in v1.

## 8. Deliverables & workflow

- **Package**: `celery-manager` — core API, REST API, dashboard SPA.
- **`examples/django_demo/`**: minimal Django project with Celery configured
  (a couple of demo tasks including a periodic one) and `celery-manager`
  mounted in — used to manually exercise the dashboard end-to-end.
- **`docs/`**: MkDocs Material site — install/quickstart, decorator usage,
  REST API reference, dashboard walkthrough, config reference (DB backends,
  auth).
- **Git workflow**: this repo's local `user.name`/`user.email` are set to the
  `pythondeveloper6@gmail.com` identity (not the machine's global config).
  One commit per logical implementation step. **No GitHub repo is created and
  nothing is pushed from this machine** — commits stay local; the user pushes
  from a separate laptop.

## 9. UI design bar

The dashboard is a primary deliverable, not an afterthought — it gets a
dedicated design pass (frontend-design skill) rather than default/generic
component styling. Distinctive visual identity, considered typography and
layout, real states designed (empty/loading/error, not just the happy path),
consistent light/dark handling. Applies to all dashboard screens listed in
§4.6, built in the same pass as their functionality — not bolted on after.

## 10. Project & work tracking

Implementation is tracked in a living `plan.md` at the project root (not a
one-shot static plan): full feature backlog including the phase 2/3 items
from §3 (so nothing discussed gets lost), current v1 work broken into
discrete todos, and status kept up to date as work proceeds. Any new request
made mid-implementation gets appended to `plan.md` (as a new todo, or a new
backlog item if it's out of scope for the current step) rather than handled
as an untracked side-note. Implementation work is executed via subagents
where tasks are independent, per the project's normal subagent-driven
workflow.

## 11. Assumptions

- Package name `celery-manager` is available on PyPI at publish time (not
  verified as part of this spec; check before the first release, not before
  building).
- Target Celery version: current stable 5.x line.
- Python version floor: 3.10+.
