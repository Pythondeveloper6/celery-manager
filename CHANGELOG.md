# Changelog

All notable changes to celery-manager are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0a1] — 2026-10-29

One report-driven release candidate. Monitoring migrations now reach `0017`.
See the [release guide](docs/release-0.3.md) for deployment, compatibility and
explicit remaining work. Live database/Redis and application rollout gates
must pass before treating this candidate as production-validated.

### Added

- Optional SQLAlchemy Celery Beat backend and stamping scheduler, explicit SQL
  authority/initialization, lossless export, reviewed adoption/repair plans,
  stale-snapshot conflicts and non-destructive reconciliation UI.
- Deterministic weekday lists/ranges, multiple daily times with a shared minute,
  exact supported time windows, readable descriptions and per-entry IANA
  timezones with preview/editor support. Unsupported meanings are refused.
- JSON arguments, keyword arguments and delivery options in schedule forms.
- Redis singleton execution leases with ownership tokens, renewal, explicit
  conflict policy, inspection and audited conditional administrative release.
- Individual/consecutive failure, stuck/lost, stale beat, SLA, regression and
  daily digest alert kinds with bounded evidence and unknown-data behavior.
- Failure acknowledgement/ignore/reopen, notes and durable review history.
- Owner-aware beat health, owner retirement and throttled external health pings.
- Minute-bucket worker heartbeat history, bounded retention, history API and UI.
- Shared-database login backoff and individually revocable local/bridge browser
  sessions, including password-change session reissue and session controls.
- “Started by me” task filtering across list/count/export/bulk cancellation,
  dispatch actor stamping, and worker-child initiator propagation.
- Validated atomic CSV import of legacy pauses, dry-run by default, via CLI/API.
- Read-only definition/history drift and opt-in observed-consumer diagnostics.
- Versioned `/api/v1` aliases, OpenAPI operations and an authenticated JavaScript
  status widget with bounded polling and safe text rendering.

### Fixed

- Windowed scheduling respects interval steps and refuses inexact boundaries.
  Per-entry zones now become due correctly, survive serialization and project
  across UTC offsets and DST changes using Celery's calendar behavior.
- Alert safety caps apply after task/time scoping, so unrelated retained history
  cannot disable a specific rule. Failure review state/history/audit commit together.
- SQL scheduler startup preserves operator-edited authoritative rows; intervals
  unsupported by upstream integer storage are refused instead of truncated.
- Login success and admission acquire locks in one order; cleanup rechecks
  expiry before deleting rows refreshed concurrently.

- Task exports preserve initiator/started-by-me filters; failure review ignores
  stale responses after switching tasks; refreshed schedule forms require an
  explicit reload before replacing newer backend arguments.
- Already-blocked login requests cannot create unlimited counterpart throttle
  rows. Singleton key callables receive consumed-metadata-free task arguments,
  and lock inspection stays inside its exact namespace.
- Beat-owner keys fit the monitoring database's 64-character key column on
  strict SQL servers. Malformed OIDC identity/role claims fail cleanly.
- Required adapter/storage CI gates reject skipped tests, cover optional
  adapters on Python 3.10 and 3.12, and verify previous-release data survives
  upgrade, rollback and repeated deployment.

### Previous release follow-up fixes


- Publishing checks whether the tested version exists on PyPI, so an unchanged
  version can publish after CI fixes. Registry errors fail the check explicitly.

- Django schedule creation consistently reports duplicate names, including
  pre-save validation failures, while preserving unrelated storage errors.
- MySQL/MariaDB task-run creation uses insert-first upserts to avoid the
  missing-key gap-lock deadlock between concurrent writers.
- Development installs include Celery's Redis transport dependencies so
  broker-related tests collect on clean Python environments.
- Django scheduler adapter tests now exercise the scheduler-owned entry cache
  that the real beat tick/sync lifecycle persists.

## [0.2.0a1] — 2026-10-28

One combined production-adoption alpha. Run migrations through `0014` before
starting application processes, or leave automatic migrations enabled. See
[production adoption](docs/production.md) and the
[requirements roadmap](docs/roadmap.md) for configuration and remaining gaps.

### Added

- Pluggable schedule discovery and CRUD: static, RedBeat and Django Celery Beat
  backends, lazy optional extras, runtime refresh, preserved removed-entry
  history, and before/after change audits. Create with natural-language or cron
  preview in the dashboard; edit timing, delete with confirmation, skip-next
  and run-now. Advanced argument/options edits are available through PATCH.
- Stamping schedulers for RedBeat and Django Celery Beat, plus `schedule_from`
  and `consume_kwargs` for migration from host-provided identity arguments.
- Deterministic English schedule parsing and optional host-provided AI parsing,
  with strict schedule validation and preview.
- OIDC bearer/JWKS authentication, a provider chain alongside existing host
  authenticators, stable audit subjects, an opt-in short session bridge,
  configurable session lifetimes, explicit trusted origins and frame ancestors.
- Configurable dispatch allowlists, strict Pydantic argument schemas, queue
  override restrictions and retry argument-edit policy shared by task starts,
  retries, failed-run reruns and schedule run-now. Denials are audited.
- Bounded log capture with head/tail retention, explicit truncation, logger and
  level filters, redaction, task-context/thread isolation and log read roles.
- Bounded task results, result opt-out, listener fallback and task-detail display.
- Public task-link helper and optional producer hook with after-publish queued
  recording and initiator headers. Unrecorded deep links wait and recover.
- Explicit polling mode for WSGI; no stream endpoint or EventSource loop in that mode.
- Worker event enabling at listener startup and every minute, with an opt-out.
- SQLAlchemy schema isolation and engine options, SQL Server migration/range
  locking and UTC server defaults. Schema-aware migration, prune and user CLI.
- Durable last-seen worker state, advisory stuck/lost-run health, optional beat
  heartbeat health, and Microsoft Teams Workflows alert channels.
- Optional-adapter CI and live PostgreSQL/MySQL/SQL Server storage contracts.
  These integrations require their CI and real-application gates; local SQL
  compilation alone does not establish live backend compatibility.

### Changed

- Only registered user tasks can be dispatched by default. Queue overrides now
  require `allow_queue_override=True`. An empty `startable_tasks` disables dispatch.
- Cancelling an unknown task ID returns 404; explicit forced cancellation is admin-only.
- Reversed `@track` / `@app.task` placement raises a clear import-time error.
- CI and docs run after changes reach main. Publishing follows successful main CI,
  checks the exact tested commit and publishes versions absent from PyPI. PR, manual and
  release-event triggers are removed.
- Existing `celery_manager.beat:DatabaseScheduler` keeps its static-editing meaning;
  the Django integration is `celery_manager.beat:DjangoDatabaseScheduler`.


## [0.1.0a5] — 2026-10-28

Run the migrations after upgrading (`0006` to `0011`; automatic unless
`auto_migrate=False`). To get exact skip matching, run beat with
`-S celery_manager.beat:Scheduler`. Everyone is signed out once: session
cookies now carry a password fingerprint, and older cookies have none.

### Added

- **Alerts.** Rules for a task's failure rate, a queue backlog, a worker
  going offline or a periodic task missing its run, each sent to Slack, a
  webhook (signed with `X-Celery-Manager-Signature` when it has a secret) or
  email. One message per incident, a quiet period, and an optional
  "resolved" message; safe with several evaluators on one database. Run the
  evaluator with `celery-manager alerts` or `celery-manager events
  --with-alerts`. New Alerts page, `/api/alerts/*` routes (writes are
  admin), migration `0009`, and config options `dashboard_url`, `smtp_*`,
  `alert_email_from`, `alert_send_timeout_seconds` and
  `alert_interval_seconds`. See `docs/alerts.md`.
- **Missed-run detection.** Scheduled runs that were due but never arrived,
  judged from beat's occurrence stamps: a "Missed runs" section on each
  schedule, a count on the schedule list, `GET /api/schedules/{name}/missed`
  and `missed_count` on `GET /api/schedules`. New
  `missed_run_grace_seconds` (default 300).
- **Prometheus metrics and OpenTelemetry.** `GET /metrics`, off until
  `metrics_token` is set, then behind `Authorization: Bearer`. Stored runs by
  task and status, a 15-minute runtime histogram and failure ratio, queue
  depth, workers online and missed runs. Optional `otel` extra with
  `celery_manager.metrics.otel.register()`. See `docs/metrics.md`.
- **Sign in with your app's users.** `authenticate_request` on the config,
  with ready-made `django_authenticator()` and `flask_authenticator()` in
  `celery_manager.integrations` that map host users to roles. Also
  `local_login_enabled`, `login_url`, `logout_url` and
  `login_provider_name`, and `GET /api/auth/options`. See
  `docs/integrations.md`.
- **User management in the dashboard.** An admin-only Users page and
  `/api/users` routes to add users, change roles, reset passwords and delete
  accounts, with the last admin protected. Every account can change its own
  password (`POST /api/me/password`). A password change ends that account's
  other sessions.
- **Edit schedules from the dashboard.** Change a crontab or interval, with
  the next five runs previewed before saving, and revert to the code's
  schedule. Opt in with `schedule_editing=True` and run beat with
  `-S celery_manager.beat:DatabaseScheduler` (it reads
  `celery_manager_db_url` from the Celery config). Migration `0008`. See
  `docs/schedules.md`.
- **Maintenance windows.** Skip chosen schedules (or all) on a recurring
  window, e.g. Sundays 02:00 for two hours, in any IANA timezone. Runs inside
  a window are recorded as skipped with a reason; nothing needs resuming.
  Migration `0010` (also adds `task_runs.skip_reason`, now set for pauses and
  single-run skips too).
- **Dead-letter view.** Failed runs nobody has retried, grouped by task and
  exception, with "edit arguments and run again" for one run or many
  (`GET /api/dead-letters`, `POST /api/dead-letters/rerun`, operator).
- **Data retention.** Delete runs after a number of days, with per-task
  overrides, a longer period for failed runs and an optional audit-log
  period, set on the Settings page. Nothing is deleted until you set it.
  Prune with `celery-manager prune [--dry-run]` or the opt-in task from
  `celery_manager.tasks.register_prune_task()`. See `docs/retention.md`.
- **Explicit occurrence identity for skips.** New beat scheduler
  `celery_manager.beat:Scheduler` (and `StampOccurrencesMixin` for custom
  schedulers) stamps each beat message with `cm_schedule` and
  `cm_occurrence` (the scheduled time of the firing, the same value the
  dashboard lists). New `task_runs.occurrence_at` and `beat_sent_at`
  columns (migration `0006`).
- **Roles: `viewer`, `operator`, `admin`.** Each account has a role, and
  every state-changing route declares the minimum it needs, checked on the
  server (`403` below it). Viewers can read everything (including task
  arguments, logs and export) and change nothing. Operators can retry,
  cancel, terminate and start tasks, pause and resume schedules and single
  runs, and use the reversible worker controls (pool grow/shrink, queue
  consume/cancel, rate limits). Admins can also purge queues, shut down
  workers and change branding. See `docs/roles.md`.
- `GET /api/whoami` and `POST /api/auth/login` return `role`. The dashboard
  hides controls the role can't use, shows the role next to the username,
  and shows the server's message when an action is refused.
- CLI: `celery-manager create-user NAME [--role ROLE] [--password-stdin]`,
  `celery-manager set-role NAME ROLE` and `celery-manager list-users`, all
  with `--db-url`. `create-user` defaults to `viewer`.
- `celery_manager.auth.users.create_user(..., role="admin")` (the default
  stays `admin` so existing setup scripts keep working),
  `set_user_role()` and `get_user_role()`; `celery_manager.api.deps`
  gains `require_role()`, `require_operator` and `require_admin`.
- `create_app()` refuses to start if a `POST`/`PUT`/`PATCH`/`DELETE`
  route doesn't declare a role, so a new route can't be left open to
  viewers by accident.

- **Broker health.** A Broker panel on the Queues page and
  `GET /api/broker/health`: on Redis, latency, version, memory against
  `maxmemory`, the eviction policy (a warning for anything but
  `noeviction`, which can silently drop queued tasks), clients, evicted
  keys and persistence; on RabbitMQ, latency and server version. The URL's
  password is masked. New metrics and two alert kinds, `broker_unreachable`
  and `broker_memory`.
- **Queue wait time.** How long runs waited in the queue before a worker
  started them (time waiting for an ETA or countdown left out), as p50/p95
  per queue on the Queues page, "waited Xs" on a run, a line on a task's
  Insights page, `celery_manager_queue_wait_seconds`, and a `queue_wait`
  alert kind. The events listener now records `sent_at`, `received_at` and
  `eta` (migration `0011`).
- **Queue depth history.** The events listener samples queue depth every
  `queue_sample_interval_seconds` (60 by default) into
  `queue_depth_samples`, kept `queue_sample_retention_days` (14). Queue
  sparklines show the last 24 hours, with a larger chart on click;
  `GET /api/queues/{name}/history`; a `queue_growing` alert kind.
- **Look inside a queue.** Click a queue's name to see the messages waiting
  in it, in the order workers will take them, without taking them off.
  Exact on Redis. On RabbitMQ it is approximate and off unless
  `queue_browser_amqp=True`, because looking marks messages redelivered.
  `queue_browser_show_args=False` hides arguments. On Redis an admin can
  delete one waiting message or move it to another known queue (audited;
  409 if a worker took it meanwhile).
- **Held tasks.** A new page with two tabs. *Delayed*: every task workers
  hold with an ETA or countdown, with a countdown, cancel and bulk cancel
  (operator; the run is marked revoked and audited). *Stuck (Redis)*:
  messages a worker took but never acknowledged, for example after a crash,
  classed as running, held by a live worker or orphaned, with how long until
  kombu redelivers them, and a restore (operator, audited) that refuses a
  task a live worker is running. `GET /api/delayed`,
  `POST /api/delayed/cancel`, `GET /api/queues/unacked`,
  `POST /api/queues/unacked/restore`.
- **RabbitMQ management integration.** Set `rabbitmq_management_url` (and
  a user with the `monitoring` tag) to get consumers, ready and
  unacknowledged messages, publish and deliver rates, queue type, max
  priority and dead-letter settings per queue, plus node memory, disk and
  alarms, on the Queues page and in `/metrics`. A `rabbitmq_alarm` alert
  kind fires while any node has a memory or disk alarm. Credentials stay in
  config and never appear in responses or logs.
- **RabbitMQ dead-letter queues.** A page listing the queues things are
  dead-lettered into (from queue arguments, policies and
  `rabbitmq_dlq_patterns`), their messages with the `x-death` reason and
  original queue (approximate: looking marks them redelivered), and an
  admin-only requeue back to where each came from. A message is acked off
  the dead-letter queue only after the broker confirmed the copy; one that
  has died more than 3 times is refused unless forced. Audited as
  `requeue_dead_letters`.
- **Docker Compose demo.** `examples/django_demo` runs the whole stack with
  `docker compose up`: Postgres, Redis (or RabbitMQ with a profile), worker,
  beat, events listener, dashboard, Mailpit, a webhook sink, Prometheus
  (profile) and generated traffic, with demo users, alert rules and a
  maintenance window.

### Fixed

- A skip or cancel no longer misses a firing that starts more than
  `eta_tolerance_seconds` late, and a manual `.delay()` near the target
  time no longer consumes it. `@track` matches the override to the stamped
  occurrence: exactly for crontab/solar, within `eta_tolerance_seconds`
  (capped at half the interval) for interval schedules. Messages without
  the headers never consume an override.
- Interval-schedule occurrences are anchored to beat's own send time when
  available, not the last run's start time, so queue delay and manual calls
  no longer shift them.

- Prefork worker processes no longer share the database connection their
  parent opened in `celery_manager.setup()`; on Postgres that caused
  `ResourceClosedError`, wrong rows and slow tasks.
- `register_prune_task(schedule=...)` no longer loses its beat entry when
  Celery settings come from Django (`CELERY_BEAT_SCHEDULE`).
- Queue depth on the filesystem transport shows as unavailable instead of
  0, and purge there is refused with the reason.

### Changed

- `eta_tolerance_seconds` is no longer compared with the task's start time,
  except as a fallback for messages without occurrence headers while beat
  has never stamped any (an older beat during a rolling upgrade). The
  worker logs a warning once when it uses that fallback.

- Migration `0007` adds `users.role`. **Every existing account becomes
  `admin`**, so nobody loses access on upgrade; demote with `set-role`.
  Rows inserted without a role get `viewer`.
- A role change or account deletion applies on the next request; the role is
  read from the database on every request, not stored in the cookie.

## [0.1.0a4] — 2026-10-26

### Added

- **Pause and resume a whole schedule.** `POST /api/schedules/{name}/pause`
  and `POST /api/schedules/{name}/resume` (also
  `celery_manager.control.schedules.pause_schedule` / `resume_schedule`).
  While a schedule is paused, every firing is recorded as a `skipped` run and
  the task body does not run — the same mechanism as an occurrence skip.
  Both endpoints are idempotent, return `404` for an unknown schedule, and
  write `pause_schedule` / `resume_schedule` audit rows. `GET /api/schedules`
  items gain `paused`, `paused_at` and `paused_by`; each occurrence from
  `GET /api/schedules/{name}/occurrences` gains `paused`. The frontend API
  client gains `pauseSchedule()` / `resumeSchedule()`. Limitations:
    - only `@track`-decorated tasks are affected — celery-manager does not
      own beat, so beat keeps firing and an undecorated task still runs;
    - resume waits for the next regular beat firing. It doesn't run the task
      now, doesn't reset beat's interval clock, and doesn't catch up firings
      missed while paused (they were recorded as `skipped`).

  The pause survives restarts: the startup sync from `beat_schedule` leaves
  it alone.

- **Pause/Resume in the dashboard.** Each schedule on the Schedules page has
  a Pause button (with a confirm step) or a Resume button and a Paused badge.
  A paused schedule's page shows who paused it and when, and marks upcoming
  runs as "Will be skipped".

- **Task list filters in the URL, saved views, copy link.** Status tab,
  search, group-by, date range and history-mode page are reflected in the
  hash-route query string (`#/?status=failed&q=smtp`), restored on load and
  on back/forward. Discrete choices push a history entry; typing in search
  replaces. **Copy link** copies the view URL. **Views** saves named filter
  sets per browser in `localStorage` (rename/delete supported; no backend).
  Filter state moved into `useTaskFilters`.

- **Export task runs.** `GET /api/tasks/export?format=csv|json` streams every
  run matching the list filters, capped at the new
  `CeleryManagerConfig.export_max_rows` (default 50000) with an
  `X-Export-Truncated` header, CSV formula-injection escaping, a timestamped
  filename, and an `export_tasks` audit row. The task list gains an Export
  menu.

- **Insights: trends, per-task pages and grouped failures.** New Insights page
  with 1h/24h/7d/30d ranges, KPIs, SVG charts (runs by status, p50/p95
  runtime, each with a text summary and table fallback) and a Failures tab
  that groups failed runs by a normalised exception signature, with open,
  show-runs and retry-all-in-group actions. A task page at
  `/task-types/:taskName` (linked from the task detail title and the Insights
  top-task lists) shows the same trends for one task plus recent runs and top
  errors. Backed by `GET /api/analytics/overview`, `/api/analytics/failures`
  and `/api/analytics/tasks/{task_name}`, computed at query time (no schema
  change; scans are capped, see [Insights](insights.md)).

- **Start a task from the dashboard.** A **Start task** button on the Task
  runs page opens a sheet with a searchable task picker, args/kwargs JSON
  editors prefilled from the task's signature with live validation, and an
  optional queue and countdown/ETA; submitting opens the new run. New
  `GET /api/tasks/registered` lists registered tasks (excluding `celery.*`)
  with best-effort signature, queue and docstring summary.
  `POST /api/tasks/start` now accepts optional `queue`, `countdown` and `eta`,
  and seeds a `queued` run row so the run's page exists straight away.
- **Retry with changed arguments.** `POST /api/tasks/{id}/retry` takes an
  optional `{"args", "kwargs"}` body (no body behaves as before); retry
  lineage is recorded as usual and the audit row notes the edit and the
  arguments sent. The task detail page gains **Retry with changes**.
- **Workflow tree for chains, groups and chords.** Runs now record Celery's
  `parent_id`, `root_id` and group id (schema revision `0003`; captured from
  task events and from `@track`'s request). `GET /api/tasks/{id}/workflow`
  returns the whole tree (capped at 500 nodes), the task detail payload gains
  `parent_id`/`root_id`/`group_id`, and the task detail page shows a
  **Workflow** card: an indented, collapsible tree with a status summary.

- **Workers page.** A new Workers page lists every worker that answers the
  broker: status, PID, uptime, pool, processed count, active/reserved/
  scheduled counts (active tasks link to their run), and consumed queues. It
  refreshes every 10 seconds while the tab is visible. From each worker card
  you can shut the worker down (with a confirm step), add or remove a pool
  process, and start or stop consuming a queue. Backed by `GET /api/workers`
  and `POST /api/workers/{worker}/shutdown`, `.../pool/grow`,
  `.../pool/shrink`, `.../queues/add` and `.../queues/cancel` (also
  `celery_manager.control.workers`). Every control is audited. If the broker
  can't be reached the endpoints answer `503` with a readable message, not a
  500. New config field `inspect_timeout_seconds` (default `1.0`) caps how
  long each broadcast waits for replies. See [Workers](docs/workers.md).

- **Runtime rate limits.** `POST /api/tasks/rate-limit` changes a task's rate
  limit on running workers (all, or a `destination` list) through
  `control.rate_limit`. The rate format (`10/s`, `10/m`, `100/h`, or `""` to
  remove the limit) is checked before anything is sent, and the change is
  audited. `GET /api/workers/rate-limits` lists registered tasks with the
  limit declared in code. On the Workers page a Rate limits table lets you
  edit a limit inline and shows which workers confirmed it. These limits are
  **not persisted**: a worker that restarts goes back to the limit declared
  in code.

- **Queue depth.** `GET /api/queues` lists every queue celery-manager knows
  of (default, `task_queues`, `task_routes`, queues seen on runs, and
  queues live workers consume). For each one it gives the broker's message
  count and consumer count, read with a passive `queue_declare`, next to the
  number of runs recorded as `queued` there. It always answers `200`: an
  unreachable or slow broker (3s deadline) or a `memory://` broker gives
  `depth: null` and a reason. A new **Queues** page in the dashboard shows
  this, refreshes every 5 seconds (paused while the tab is hidden) and draws
  a sparkline of depth over the session. See `docs/queues.md`.

- **Purge a queue.** `POST /api/queues/{name}/purge` removes the ready
  messages from one queue and returns how many. If it removed any, runs
  recorded as `queued` in that queue without an ETA are marked `revoked`,
  since no event will ever arrive for them. Audited as `purge` with both
  counts. In the dashboard you confirm by typing the queue name.

- **Bulk cancel.** `POST /api/tasks/cancel-bulk` revokes a list of
  `task_ids`, or every `queued`/`started`/`retried` run matching the task
  list's filters. It is capped at 1,000 (above that it's refused, not cut
  short), has a `dry_run` count and an opt-in `terminate`, and writes one
  `cancel_bulk` audit row. The task list gains **Cancel N selected** on the
  Queued, Started and Retried tabs, and **Cancel all matching…** with the
  count shown before you confirm.

- **Pause a schedule until a set time.** `POST /api/schedules/{name}/pause`
  takes an optional `{"until"}` (future, UTC; past is a `422`) and the
  schedule resumes by itself afterwards, with no background job: readers
  clear the stale pause lazily and write one automatic `resume_schedule`
  audit row. Re-pausing with a new `until` updates it. `paused_until` joins
  the schedule payloads, and each occurrence's `paused` is now per occurrence.
  The dashboard's pause button becomes a menu (indefinitely, 1 hour, until
  tomorrow 9:00, custom time). Schema revision `0004`.

### Changed

- **Dashboard redesign.** shadcn/ui-style look (zinc theme, Geist fonts) in
  light and dark. The sidebar collapses to icons (Ctrl/Cmd+B, remembered per
  browser) and slides in over the page on phones. The task list gains a run
  strip (one bar per run, coloured by status), status tabs and a search and
  filter bar. Task detail puts the failure and logs first with execution
  details alongside. Tables stack on narrow screens.
- **Schema migrations with Alembic.** celery-manager's tables are now
  versioned, and the migrations ship inside the package (no `alembic.ini`
  needed). By default `init_db()` upgrades the database at startup, under a
  lock so the API, workers and listener can start together. Databases from
  0.1.0a1–0.1.0a3 are adopted automatically and keep their data. The new
  `auto_migrate` config field (default `True`) turns this off for teams that
  migrate as a deploy step with the new `celery-manager migrate --db-url ...`
  command (`--check` reports status and exits 1 if an upgrade is needed).
  The version is kept in `celery_manager_alembic_version`, so it doesn't
  clash with a host app's own Alembic history. New dependency: `alembic`.
  This release's schema changes are migrations `0002`–`0005`: schedule pause
  (`paused_at`, `paused_by`), workflow lineage (`parent_id`, `root_id`,
  `group_id`), `paused_until`, and `task_runs.updated_at`. The database user needs DDL
  rights when a release includes a migration, or run `migrate` with a user
  that has them. See [Schema and upgrades](docs/configuration.md#schema-and-upgrades).
- **Live updates now work when the events listener runs as a separate
  process.** Previously `/api/stream` connected and then sent nothing in the
  documented deployment, and the dashboard only kept up by polling. Each API
  process now polls a change feed in the database: a new indexed
  `task_runs.updated_at` column that every write path bumps (migration
  `0005`, backfilled from each run's latest timestamp). Changes made by the
  listener, `@track`, `report_progress()` and retry/start reach the browser
  within about a second, with several API processes too, and no new
  infrastructure. Polling happens only while a dashboard is connected; the
  new `live_poll_interval_seconds` config field (default `1.0`) sets the
  interval. Stream events are now `{"type": "task-updated", "task_id",
  "status", "updated_at"}`, one per changed run. The task list and task
  detail pages poll only while the stream is down, and the header indicator
  shows **Live** only while it is actually connected (**Reconnecting**
  otherwise). `EventListener` no longer publishes events itself; its
  `event_bus` argument is ignored.

## [0.1.0a3] — 2026-10-25

### Fixed

- **Interval-schedule occurrences** (`run_every` / seconds / `timedelta`) are
  now anchored to the schedule's last observed run instead of "now + interval".
  Skips on interval schedules display stably across page loads and actually
  fire against beat's real run times.
- **Race-free `task_runs` writes.** The events listener, the `@track`
  decorator, `report_progress()` and the retry button used to check-then-insert
  the same row. They now share one race-safe get-or-create (native upsert on
  SQLite/PostgreSQL/MySQL, SAVEPOINT fallback elsewhere), so:
    - retry no longer returns a 500 or loses its lineage when the worker's row
      lands first;
    - the listener no longer leaves `worker`/`queue` empty when it loses the race;
    - late or out-of-order events can no longer downgrade a run's status;
    - the producer's hostname from `task-sent` is no longer recorded as the worker;
    - event arguments are stored as JSON, so retries from listener-created rows
      decode correctly.

### Added

- Documentation site: <https://pythondeveloper6.github.io/celery-manager/>.
- Dashboard screenshots in the README and docs, and an "Interval schedules"
  section in the dashboard guide.
- The race tests run against PostgreSQL in CI (`CM_TEST_DB_URL`).

### Changed

- README and docs no longer say tasks can be started from the UI; starting is
  API-only (`POST /api/tasks/start`).

## [0.1.0a2] — 2026-10-25

The first release published to PyPI (`pip install --pre celery-manager`).
There are no code changes since 0.1.0a1 apart from the version string.

### Changed

- README and package metadata are PyPI-ready: every link and image in the
  README is now absolute, so it renders on pypi.org.
- Install instructions lead with `pip install --pre celery-manager`; the git-tag
  install remains as an alternative.
- Added a `Changelog` project URL; the GitHub account casing is now
  `Pythondeveloper6` everywhere.
- `SECURITY.md` updated for the now-public repository.

## [0.1.0a1] — 2026-10-25

The initial release. Before this alpha, celery-manager had never been published, so everything
below is new rather than changed — with a few exceptions, called out first
because anyone who pinned a git SHA during development *will* hit them.

### Breaking

- **`GET /api/tasks` now returns an envelope, not a bare array.** It used to
  return a JSON array of runs, hard-capped at 50 rows, with no `offset` — so
  older history was unreachable and a client could not tell "50 results" from
  "the first 50 of 1,284". It now returns:

  ```json
  {"items": [ ... ], "total": 1284, "limit": 50, "offset": 0}
  ```

  Callers must change `res.json()` to `res.json().items`. The items also
  gained `runtime_seconds` and the four `progress_*` fields. There is no
  compatibility shim and no version negotiation: the change was taken
  deliberately while the package was unreleased and its only consumers were
  the bundled dashboard and `docs/rest-api.md`. See
  [the REST API reference](docs/rest-api.md#tasks).
- **The session cookie is now `cm_session`, and `create_app()` refuses a
  default `secret_key`.** Both are security fixes; see [Security](#security)
  below for what changed and what you need to do.

### Added

**Task history, search and filtering**

- `GET /api/tasks` gained `q`, `date_from`, `date_to`, `limit` and `offset`
  alongside the existing `status` and `task_name`. `limit` is clamped into
  `1..500` (`control.tasks.MAX_PAGE_SIZE`) rather than rejected, and the
  clamped values are echoed back in the envelope.
- `q` is a case-insensitive substring search OR-ed across `task_name`,
  `task_id`, `args` and `kwargs`. Because args/kwargs are stored as
  serialized JSON text, this searches that text — there is no way to express
  `kwargs.user == "alice"`. LIKE wildcards in the search term are escaped, so
  `q=100%` finds a literal `100%` instead of matching every row.
- Both date bounds apply to `started_at` and are inclusive, which means a
  queued run (no `started_at`) is excluded by either bound, and `date_to` is
  an instant rather than a calendar day.
- `control.count_task_runs()` — the total behind "showing 50 of 1,284". It
  shares one filter builder with `list_task_runs()`, so a total can never be
  a count of a different query than the page it labels.

**Live progress**

- `celery_manager.report_progress(current, total=None, message=None)`, called
  from inside a task body. Writes `progress_current`, `progress_total`,
  `progress_message` and `progress_updated_at` on the run's `task_runs` row
  while the task is still in flight; all four are served raw by both task
  endpoints so the client decides how to render them.
- Database writes are throttled to one per 0.5 s per run
  (`progress.MIN_WRITE_INTERVAL_SECONDS`), so it is safe in a tight loop. The
  completing call (`current >= total`) bypasses the throttle, so a finished
  task never sits at 99%.
- Omitting `total` means open-ended progress: a count and a message, no
  percentage. A `total` of 0, negative, or non-int-able is treated the same
  way — celery-manager never invents a denominator.
- Never raises into the calling task, and never blocks it: bad arguments are
  ignored, a missing task context or an unwired process each log one WARNING
  per process and return.
- Works on undecorated tasks too, via `celery.current_task`.

**Retry lineage**

- `task_runs.retried_from_id`, stamped by `retry_task()` on the run it
  dispatches, so the chain is visible immediately rather than whenever the
  events listener catches up.
- `control.list_retries_of()` and the `retried_from_id` / `retried_by` fields
  on `GET /api/tasks/{task_id}`. `retried_by` is a list — retrying the same
  failed run more than once is normal, and each attempt is its own run.

**Branding settings**

- `GET` / `PUT /api/settings` and the `app_settings` table, holding exactly
  three keys: `brand_name`, `brand_accent`, `brand_logo_url`. Deployment-wide,
  not per-browser.
- **`GET /api/settings` is deliberately unauthenticated** — the login screen
  has to render the deployment's branding, and the login screen is pre-auth.
  That is safe only because `DEFAULT_SETTINGS` is a closed allowlist of
  non-sensitive values; `updated_by`/`updated_at` are deliberately not
  returned. Treat adding a key to that dict as a security decision.
- Values are validated strictly and rejected with a `422` carrying a
  human-readable reason: the accent is an anchored hex allowlist (it lands in
  a CSS custom property), and the logo URL is a scheme allowlist that refuses
  SVG data URIs (it lands in an `<img src>`).
- Any authenticated user can rebrand — there is no role system in v1 — but
  every changed key writes an audit row with old and new values.
- `control.get_settings()` / `control.update_settings()` for the same
  operations from Python.

**Task detail depth**

- `GET /api/tasks/{task_id}` gained `queue`, `worker`, `eta`, `started_at`,
  `finished_at`, `runtime_seconds` and the four `progress_*` fields. (`eta`
  is served but nothing currently writes it — see the note in the REST API
  reference.)

**Dashboard**

- Task list rebuilt around a left filter panel (search, status, group-by,
  collapsible date range) with removable active-filter chips, repeated in the
  empty state so an empty table says which filter emptied it. The panel
  collapses behind a **Filters** toggle below ~1024px.
- Two explicit modes: **live** (polls every 5 s, **Load more** widens the
  page up to the 500-row ceiling) and **history** (explicit offset paging
  through the full filtered total, polling switched off so rows can't shift
  under you between clicks), with a **Back to live** bar.
- Per-row progress bars with percentage, message and a projected time
  remaining; a run that has stopped reporting says how long it has been quiet
  and drops the estimate rather than counting down as if nothing were wrong.
- Task detail gained a Run panel (progress, live elapsed, estimated finish),
  an Execution panel (worker, queue, ETA, timings, each with a reason rather
  than a blank cell), and a retry-lineage panel walking the chain in both
  directions.
- Light / dark / **system** theming as an explicit per-browser choice, stored
  in local storage, applied before first paint and synced across tabs.
- A branding settings screen with a live preview and a measured contrast
  readout for the accent colour.
- Dashboard: sign-out control in the nav.
- Dashboard: Cancel and Terminate controls on the task detail page (terminate asks for inline confirmation)

**Framework integration**

- Mount the dashboard under a prefix inside a FastAPI app
  (`app.mount("/celery-manager", dashboard)`) or a Flask app (via `a2wsgi`
  and werkzeug's `DispatcherMiddleware`, new `flask` extra).
- The SPA now uses relative asset and API URLs, so it works under any mount
  prefix.
- Runnable `examples/fastapi_demo` and `examples/flask_demo`.
- New docs page `docs/integrations.md`.

**Packaging and docs**

- MkDocs Material documentation site under `docs/`.
- Root `README.md`, PyPI classifiers, keywords and project URLs; `uvicorn`
  moved into a `standalone` extra since mounting the ASGI app into a server
  you already run needs no server of its own.
- MIT license.

### Changed

- **`@track` now writes a `started` row before calling your task body**, not
  only a terminal row afterwards. This is what makes an in-flight task
  visible with no events-listener process running, and what gives
  `report_progress()` a row to write to. **Cost: one extra INSERT/UPDATE and
  commit per tracked run.** As with every other write in the decorator, a
  failure is logged and dropped and can never change the task's outcome.
- `POST /api/schedules/{name}/occurrences/pause` now rejects
  `action="reschedule"` with a `422`. It used to accept and store it, which
  displayed the occurrence as paused while doing nothing — the decorator only
  ever acted on `skip`/`cancel`. The column still permits the value; the
  endpoint no longer does.
- The session cookie is now `Secure` by default (`cookie_secure=True`). Set
  it to `False` only for local plain-HTTP development, where otherwise you
  will log in successfully and then immediately appear logged out.
- `beat_schedule` entries are normalized through
  `celery.schedules.maybe_schedule()`, so a bare number of seconds or a
  `timedelta` works the same as an explicit `schedule()`/`crontab()`.
- `self.retry()` is now recorded as `retried` rather than `failed`. It raises
  `celery.exceptions.Retry`, an ordinary `Exception` subclass, which used to
  land in the decorator's failure branch and give a run Celery considers
  RETRY a `failed` status, a "Retry in 30s: …" exception message and a finish
  time it did not have.
- A retry attempt now clears what the previous attempt left on the shared row
  (`exception`, `traceback`, `finished_at`, `runtime_seconds`, the four
  `progress_*` columns). A task that failed once and then succeeded used to
  render as `succeeded` with a stale traceback and a stale progress bar.
- `POST /api/tasks/{id}/retry` returns `404` for an unknown task id instead of
  an unhandled `ValueError` (a `500`); `retry-bulk` does the same.
  `control.TaskRunNotFound` is a `ValueError` subclass, so Python callers
  already catching `ValueError` are unaffected.

### Fixed

- The events listener no longer downgrades a decorator-owned `skipped` status.
  A skipped occurrence returns normally, so the worker emits an ordinary
  `task-succeeded` for the same uuid — which used to flip the row back to
  `succeeded`, making a paused occurrence look as if it had run.
- `EventBus.publish()` is thread-safe: subscribers are stored with the event
  loop that owns their queue, and publishes are handed back to that loop via
  `call_soon_threadsafe`.
- The decorator upserts `task_runs` instead of inserting, so it can't raise
  `IntegrityError` when the events listener has already written a row for the
  same uuid.
- Schedule overrides are matched by an `eta_tolerance_seconds` window rather
  than exact timestamp equality, and each override is claimed by at most one
  occurrence — so a paused occurrence keeps showing as paused, and can't show
  as paused twice on a tightly-spaced schedule.
- Task timestamps are stamped back to UTC at the API boundary. SQLite drops
  `tzinfo` on a `DateTime(timezone=True)` round-trip, so they used to reach
  JSON unmarked and be parsed as *local* time by `new Date(...)`. The
  `date_from`/`date_to` query parameters are normalized the same way on the
  way in — an offset-carrying bound used to be compared by its wall-clock
  fields, shifting the window by the caller's offset. `GET /api/audit`'s
  `created_at` is stamped the same way.
- `GET /api/audit` clamps `limit` into `1..500`. SQLite reads `LIMIT -1` as
  *no limit*, so `?limit=-1` returned the entire audit log in one response.
- The decorator no longer fails a task whose args it cannot JSON-encode. It
  serializes args/kwargs *before* running the body, and a `datetime` kwarg —
  supported by Celery's own serializer, rejected by stdlib `json.dumps` —
  used to raise out of the wrapper and mark the task FAILURE without ever
  running it. Unencodable leaves are now stored as their `repr`.
- The task list and the task detail page can no longer quote different
  percentages for the same run: the rounding rule (a running task stops at
  99% until it is genuinely complete) now lives in one shared derivation.
- A non-JSON SSE frame — a proxy's keep-alive comment, a truncated write — no
  longer throws an uncaught error out of the event handler.
- The task detail page no longer repaints with a stale response when you
  navigate from one live run to another while a fetch is in flight.
- The audit log and both schedule screens show the server's own error message
  with a **Try again** button instead of a fixed sentence with nothing to
  click.
- Retrying a run recorded only by the events listener no longer 500s. The
  listener stores args/kwargs as a Python repr rather than JSON, so
  `json.loads` failed for every undecorated task; the retry path now tries
  JSON, then `ast.literal_eval`, and refuses rather than guessing if neither
  works.
- `celery-manager events --celery-app module:app` now imports the app from the
  current directory (console scripts don't put it on `sys.path`).
- SSE stream sends a `: keepalive` comment every 15s when idle so proxies don't
  close it.

### Security

- **`create_app()` refuses to start with an empty or default
  (`"change-me"`) `secret_key`**, raising `ValueError`. The default is
  public, so a dashboard running on it accepted a session cookie anyone
  could sign for any username. Workers and the events listener, which never
  sign sessions, are unaffected. Generate a key with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **`GET /api/stream` now requires a session**, like every other non-login
  endpoint. It used to stream every task id and lifecycle event to anyone.
- **Login no longer reveals whether a username exists.** A missing user now
  pays for a bcrypt check against a dummy hash, so both cases take the same
  time (was ~0.2 s vs ~2 ms). A password over bcrypt's 72-byte limit is a
  `401` for every username instead of a `500` for existing ones only.
- `create_user()` refuses an empty password (previously accepted) and one
  over 72 bytes UTF-8-encoded with a clear `ValueError` — rather than
  bcrypt's own error on bcrypt ≥ 5, or silent truncation to 72 bytes on
  older bcrypt.
- A session cookie for a user that has since been deleted is rejected with
  a `401`; it used to keep working until it expired.
- **Breaking for anyone on a dev SHA: the session cookie is renamed from
  `session` to `cm_session`**, so it no longer collides with Flask's or
  Starlette's own `session` cookie when celery-manager is mounted inside
  another app. Its `Path` is now the app's mount point (`root_path`) rather
  than always `/`. Existing sessions are not carried over — everyone signs in
  once more, and the stale `session` cookie simply expires.
- Logout deletes the cookie with the same `Path`, `HttpOnly`, `SameSite=lax`
  and `Secure` attributes it was set with.
- Cross-origin `POST`/`PUT`/`PATCH`/`DELETE` requests are refused with a
  `403` when their `Origin` doesn't match `Host` (or `X-Forwarded-Host`).
  Requests without `Origin` — curl, scripts — are unaffected. A proxy that
  rewrites `Host` must forward the original as `X-Forwarded-Host`.
- Every response carries `X-Frame-Options: DENY` and
  `Content-Security-Policy: frame-ancestors 'none'` against clickjacking.
- The `celery-manager events` CLI no longer accepts `--secret-key`, which it
  never used. Drop it from any command line that passes it.
- New [Security model](docs/configuration.md#security-model) section in the
  configuration docs: every account is a full administrator, there is no
  login rate limiting, logout does not revoke a stolen cookie, and
  `brand_logo_url` may point at an external host.

### Known limitations

- **SSE only carries web-process events.** `EventBus` is in-memory in the API
  process, so with the documented deployment (events listener as a separate
  process) `/api/stream` never delivers the listener's task events. The
  dashboard's 5-second poll keeps it current. Don't build on `/api/stream` yet.
- **No schema migrations.** `init_db()` is `create_all` only; it creates
  missing tables and never alters existing ones.
- **No role system.** Any authenticated user can perform any control action,
  including dispatching any task name with any arguments.
- **No login rate limiting.** Rate-limit `POST /api/auth/login` at your
  reverse proxy.
- **Sessions can't be revoked individually.** They are stateless signed
  tokens valid for 7 days; rotating `secret_key` revokes all of them.
- `GET /api/settings` is unauthenticated by design (the login screen needs
  the branding).
- `POST /api/tasks/retry-bulk` is not partial-failure aware: it stops at the
  first unknown id and reports a `404` for the whole batch, after having
  already dispatched the ids ahead of it.
- `task_runs.eta` is served by the detail endpoint but never written by
  anything, so it is always `null`.
- Revoked runs have no `finished_at` (only `task-succeeded`/`task-failed` set it).
- Dispatching an unregistered task name records `task_name` as `"unknown"` when
  the listener creates the row.
- The events listener needs `worker_send_task_events` (or `-E`) and
  `task_send_sent_event` to be set; without them `worker`/`queue` are null. See
  [Required Celery settings](docs/quickstart.md#required-celery-settings).
- **macOS prefork workers fail every task** (`ValueError: not enough values to
  unpack`). Upstream Celery 5.6 / billiard 4.3 bug, not celery-manager. Start
  the worker with `FORKED_BY_MULTIPROCESSING=1`, or use `--pool=threads` /
  `--pool=solo` for local dev. See the [quickstart](docs/quickstart.md#required-celery-settings).
- Under Flask (a2wsgi) the SSE stream leaks a subscriber per disconnect, so
  the Flask demo disables `/api/stream` and the dashboard falls back to
  polling. Run the dashboard as its own uvicorn process for live push.

[0.2.0a1]: https://github.com/Pythondeveloper6/celery-manager/releases/tag/v0.2.0a1
[0.1.0a5]: https://github.com/Pythondeveloper6/celery-manager/releases/tag/v0.1.0a5
[0.1.0a4]: https://github.com/Pythondeveloper6/celery-manager/releases/tag/v0.1.0a4
[0.1.0a3]: https://github.com/Pythondeveloper6/celery-manager/releases/tag/v0.1.0a3
[0.1.0a2]: https://github.com/Pythondeveloper6/celery-manager/releases/tag/v0.1.0a2
[0.1.0a1]: https://github.com/Pythondeveloper6/celery-manager/releases/tag/v0.1.0a1
