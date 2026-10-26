# Changelog

All notable changes to celery-manager are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  This release's only schema change adds two nullable columns,
  `periodic_schedules.paused_at` and `paused_by`. The database user needs DDL
  rights when a release includes a migration, or run `migrate` with a user
  that has them. See [Schema and upgrades](docs/configuration.md#schema-and-upgrades).

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

[0.1.0a3]: https://github.com/Pythondeveloper6/celery-manager/releases/tag/v0.1.0a3
[0.1.0a2]: https://github.com/Pythondeveloper6/celery-manager/releases/tag/v0.1.0a2
[0.1.0a1]: https://github.com/Pythondeveloper6/celery-manager/releases/tag/v0.1.0a1
