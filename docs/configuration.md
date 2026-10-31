# Configuration reference

Every celery-manager process — the API app, the `celery-manager events`
listener and the `celery-manager alerts` evaluator — is configured through a single dataclass,
`celery_manager.config.CeleryManagerConfig`:

```python
from celery_manager.config import CeleryManagerConfig

config = CeleryManagerConfig(
    celery_app=your_celery_app,
    db_url="sqlite:///celery_manager.db",
    # Placeholder — replace with your own private random value, e.g. from
    # python -c "import secrets; print(secrets.token_urlsafe(32))"
    secret_key="<your-generated-secret>",
    eta_tolerance_seconds=60,
    missed_run_grace_seconds=300,
    cookie_secure=True,
    auto_migrate=True,
    inspect_timeout_seconds=1.0,
    schedule_editing=False,
)
```

| Field | Type | Default | What it affects |
|---|---|---|---|
| `celery_app` | `celery.Celery \| None` | `None` | `None` enables monitoring-only host-job observations; Celery execution/broker/schedule controls return 409. Supply your application's Celery instance for task introspection (dispatching tasks via `send_task`, reading `beat_schedule` to compute upcoming occurrences) and control calls (`revoke`/`terminate`). The API app and the events listener must both be given the *same* Celery app configuration (same broker, same `beat_schedule`) or their views of the world will disagree. |
| `db_url` | `str` | `"sqlite:///celery_manager.db"` | SQLAlchemy connection URL for celery-manager's own storage (task runs, schedules, overrides, users, audit log — see `celery_manager.db.models`). Any SQLAlchemy-supported backend works; swap to Postgres/MySQL/etc. for production. For `sqlite:///:memory:` specifically, the engine is built with `StaticPool` so every connection shares one in-memory database — needed for it to behave sensibly under FastAPI's threadpool (as used in the test suite); other SQLite URLs use `check_same_thread=False` instead. Every process that needs to see the same data (API app + events listener) must point at the same `db_url`. |
| `secret_key` | `str` | `"change-me"` (a placeholder) | Signs session cookies for the built-in cookie-based auth (`itsdangerous.URLSafeTimedSerializer`, sessions valid for `session_max_age_seconds`, default 7 days). Anyone who knows the key can forge a valid session cookie for any username, so **`create_app()` raises `ValueError` and refuses to start if it is empty or still `"change-me"`** — set a private random value in every process that serves the dashboard, local development included. Processes that don't serve HTTP (Celery workers calling `celery_manager.setup()`, the events listener) never sign sessions and may leave the default. Rotating it invalidates every existing session (users are signed out). See [`secret_key` in production](#secret_key-in-production). |
| `eta_tolerance_seconds` | `int` | `60` | How far apart two timestamps of the same occurrence may be: an interval-schedule occurrence stamped by beat vs. a saved `target_time` (in `@track`, capped at half the interval), and a saved `target_time` vs. a recomputed occurrence (in `GET /api/schedules/{name}/occurrences`). It is compared with "now" only for messages without occurrence headers while beat has never stamped any (see [How the skip/cancel check works](decorator.md#how-the-skipcancel-check-works)). See the trade-off below. |
| `missed_run_grace_seconds` | `int` | `300` | How long after a scheduled run was due it may still arrive before it is reported as missed (`GET /api/schedules/{name}/missed`, the `missed_count` on `GET /api/schedules`). Detection needs beat to run the stamping scheduler (see [Missed-run detection](decorator.md#missed-run-detection)). Raise it if your broker or workers are routinely slow to start a run. |
| `cookie_secure` | `bool` | `True` | Sets the `Secure` flag on the session cookie, so browsers only ever send it back over HTTPS. See below for why the default is `True` and when to change it. |
| `export_max_rows` | `int` | `50000` | Hard cap on rows in one `GET /api/tasks/export` download. Larger results are cut to the newest rows and flagged with `X-Export-Truncated: true`. |
| `live_poll_interval_seconds` | `float` | `1.0` | How often each API process polls `task_runs` for changes to push down `/api/stream` while at least one dashboard has it open. Each poll is a small query on an indexed column; nothing is polled while nobody is connected. Lower means quicker live updates and more queries. See [REST API: how changes reach the stream](rest-api.md#how-changes-reach-the-stream). |
| `auto_migrate` | `bool` | `True` | Whether `init_db()` upgrades the database schema to the latest revision at startup. Set it to `False` to run `celery-manager migrate` as a deploy step instead; `init_db()` then leaves the database untouched and logs a warning if the schema isn't current. See [Schema and upgrades](#schema-and-upgrades). |
| `metrics_token` | `str \| None` | `None` | Bearer token for `GET /metrics`. `None` disables the endpoint (it answers `404`). When set, scrapers must send `Authorization: Bearer <token>`. See [Metrics](metrics.md). |
| `metrics_cache_seconds` | `float` | `5.0` | How long the computed metrics snapshot is reused, so many scrapers cost one database pass and one broker round trip per interval. `0` disables the cache. |
| `inspect_timeout_seconds` | `float` | `1.0` | How long the [Workers page](workers.md) and the worker control endpoints wait for workers to reply to a broadcast (`inspect()`, `pool_grow`, `rate_limit`, ...). The HTTP request blocks for it, so keep it short; raise it only if workers on a slow or distant broker show as missing when they are running. |
| `schedule_editing` | `bool` | `False` | Lets operators change a schedule's crontab or interval from the dashboard. Leave it off unless beat runs `celery_manager.beat:DatabaseScheduler`: no other scheduler reads the edits, so the dashboard would list runs that never happen. While it's off, `PUT /api/schedules/{name}/schedule` answers `409`, saved edits are ignored when listing upcoming runs, and the dashboard's editor can only preview and revert. See [Editing schedules](schedules.md). |
| `dashboard_url` | `str \| None` | `None` | The dashboard's public address, e.g. `https://ops.example.com/celery/`. [Alert](alerts.md) messages link to the page about the problem (the task, queue, worker or schedule) under it. Without it, messages carry no link: the alerts evaluator runs outside the web server and can't know the address people use. |
| `smtp_host` | `str \| None` | `None` | SMTP server for [email alert channels](alerts.md#email). `None` disables email: an email channel's messages fail with that explanation, shown on the channel. |
| `smtp_port` | `int` | `587` | SMTP port. |
| `smtp_username` | `str \| None` | `None` | SMTP login. `None` sends without authenticating. |
| `smtp_password` | `str \| None` | `None` | SMTP password, used with `smtp_username`. Only ever lives here: never in the database, never in an API response, and not in the config's `repr()`. |
| `smtp_use_tls` | `bool` | `True` | Upgrade the SMTP connection with STARTTLS before logging in. Turn off only for a local relay. |
| `alert_email_from` | `str \| None` | `None` | From address of alert emails. Required for email channels. |
| `alert_send_timeout_seconds` | `float` | `10.0` | Time limit for each outbound alert delivery: one Slack post, one webhook call, one SMTP session. A channel that doesn't answer in time is recorded as failed and the other channels are still tried. |
| `alert_interval_seconds` | `float` | `60.0` | How often the alerts evaluator checks every rule (`celery-manager alerts`, or `events --with-alerts`; `--interval` overrides it). |
| `queue_sample_interval_seconds` | `float` | `60.0` | How often the events listener records every queue's broker depth, for the [depth charts](queues.md#depth-history) and the `queue_growing` alert. `0` turns sampling off. |
| `queue_sample_retention_days` | `int` | `14` | How long depth samples are kept. The retention prune and the sampler itself (hourly) delete older ones. `0` keeps them forever. |
| `authenticate_request` | callable or `None` | `None` | Sign people in with your Django, Flask or FastAPI app's users. Takes the Starlette `Request`, returns `ExternalUser(username, role)` or `None` (not signed in to your app). Asked first on every request; `None` falls back to local accounts. `role=None` is a `403`; an unknown role, a wrong return type or an exception is a `500`, never access. Ready-made: `celery_manager.integrations.django.django_authenticator()` and `celery_manager.integrations.flask.flask_authenticator(...)`. See [Sign in with your app's users](integrations.md#sign-in-with-your-apps-users). |
| `local_login_enabled` | `bool` | `True` | Whether celery-manager's own accounts can sign in. `False` (only with `authenticate_request`) replaces the password form with "Sign in with ...", refuses `POST /api/auth/login` and ignores existing `cm_session` cookies. `create_app()` refuses `False` without an authenticator. |
| `login_url` | `str` or `None` | `None` | Your app's login page, for the "Sign in with ..." link. `{next}` is replaced with the dashboard's address. A path or an `http(s)` URL; anything else makes `create_app()` refuse to start. |
| `logout_url` | `str` or `None` | `None` | Your app's sign-out page, linked from "Sign out" for people your app signed in. Without it they get no sign-out item. Same rules as `login_url`. |
| `login_provider_name` | `str` | `"your app"` | What the sign-in screen and sidebar call your app ("Sign in with Acme"). |
| `queue_browser_show_args` | `bool` | `True` | Show task arguments when [browsing a queue's waiting messages](queues.md#looking-inside-a-queue). Every role sees them, as on task runs (see [Roles](roles.md#what-viewers-can-see)). `False` leaves `args`/`kwargs` out of the response for everyone, for deployments whose task arguments carry secrets. |
| `queue_browser_amqp` | `bool` | `False` | Allow browsing RabbitMQ queues. Off by default because RabbitMQ can only show a message by handing it out: each one looked at is taken without acknowledgement and put back, which marks it redelivered and may move it to the front. While off, browsing a RabbitMQ queue answers `supported: false` with the reason. Redis browsing is read-only and needs no switch. |
| `rabbitmq_management_url` | `str \| None` | `None` | The [RabbitMQ management API](rabbitmq.md), e.g. `http://rabbit:15672`. Turns on the RabbitMQ panel on the Queues page, the `rabbitmq_*` metrics, the `rabbitmq_alarm` alert kind and the dead-letter queue page. Off while `None`, and always off when the broker isn't RabbitMQ. The vhost is taken from the broker URL. Redirects are never followed. |
| `rabbitmq_management_username` | `str \| None` | `None` | Management API login. The `monitoring` tag is enough to read; browsing a dead-letter queue also needs read permission on it; requeue uses the broker URL's user ([permissions](rabbitmq.md#the-management-user)). |
| `rabbitmq_management_password` | `str \| None` | `None` | Its password. Only ever lives here: never in the database, an API response, a log line or the config's `repr()`. |
| `rabbitmq_management_verify_tls` | `bool` | `True` | Verify the management API's certificate on `https` URLs. Turn off only for a test broker with a self-signed certificate. |
| `rabbitmq_management_timeout_seconds` | `float` | `3.0` | Time limit for one management API operation, all its HTTP calls together. |
| `rabbitmq_dlq_patterns` | `list[str]` | `["*.dlq", "*.dead", "dlq.*"]` | Queue-name patterns that count as [dead-letter queues](rabbitmq.md#dead-letter-queues) even when no queue's dead-letter exchange routes to them. |

The fields above are the whole dataclass — `CeleryManagerConfig` has no
other options, and nothing else in celery-manager reads configuration from
the environment.

The beat process doesn't use `CeleryManagerConfig`. When beat runs
`celery_manager.beat:DatabaseScheduler`, that scheduler reads two keys from
your Celery app's own config:

| Celery setting | Default | What it does |
|---|---|---|
| `celery_manager_db_url` | *required* | celery-manager's database, the same URL as `db_url`. Beat only reads the saved schedule edits from it. |
| `celery_manager_schedule_refresh` | `10` | Seconds between reads of the saved edits. An edit reaches beat within this time. |

See [Editing schedules](schedules.md).

[Data retention](retention.md) is deliberately not a config field: it is
changed at runtime from the Settings page and stored in the database.

## Tunables that are *not* config fields

Two limits that operators reasonably go looking for on `CeleryManagerConfig`
are not there. They are module-level constants, deliberately: both are
implementation limits with one sensible value, and threading them through the
config would imply a per-deployment decision that neither really is.

| Constant | Module | Value | What it does |
|---|---|---|---|
| `MIN_WRITE_INTERVAL_SECONDS` | `celery_manager.progress` | `0.5` | Minimum seconds between database writes for one run's [live progress](progress.md). The completing call ignores it. |
| `MAX_PAGE_SIZE` | `celery_manager.control.tasks` | `500` | Ceiling that `GET /api/tasks` clamps a caller-supplied `limit` to. Applied at the HTTP boundary only — a Python caller of `list_task_runs` is trusted and may ask for more. |

Both are plain module attributes read at call time, so a deployment with an
unusually loaded database can raise the throttle by assigning to it before
any task runs:

```python
from celery_manager import progress

progress.MIN_WRITE_INTERVAL_SECONDS = 2.0
```

That is a supported escape hatch rather than a documented API — if you find
yourself needing it in production, say so on the issue tracker and it can
become a real setting.

## Choosing `eta_tolerance_seconds`

Both the read side (matching a paused occurrence back to its row when the
dashboard recomputes upcoming occurrences) and the write side (the
decorator deciding whether *this* run corresponds to a paused occurrence)
match by "close enough" rather than exact timestamp equality, because
occurrence timestamps are computed independently each time rather than
stored once. That means the tolerance window is a genuine trade-off, not
just a fudge factor:

- **Too small** (e.g. under a second) risks an interval-schedule skip not
  matching beat's real firing. With beat running
  `celery_manager.beat:Scheduler`, queue delay no longer matters: the
  decorator compares the occurrence beat stamped, not the start time.
  Without it (old beat), the window is still compared with "now", and a
  task picked up outside it runs instead of being skipped.
- **Too large** (e.g. tens of minutes) on a tightly-spaced periodic
  schedule (interval shorter than `2 * eta_tolerance_seconds`) makes a
  single override ambiguous between two neighboring occurrences. The
  read-side matching in `list_upcoming_with_overrides` handles this by
  claiming each override for at most one occurrence (closest match, removed
  from the pool before matching the next occurrence), but a tolerance
  window comparable to or larger than the schedule's own interval is a
  sign it should be tightened.

The default of 60 seconds is a reasonable middle ground for schedules on
the order of minutes or longer. If you have periodic tasks firing more
than once a minute, consider a smaller value.

## `db_url` backend notes

- **SQLite** (`sqlite:///path.db` or `sqlite:///:memory:`) — zero setup,
  fine for local development, demos, and single-process deployments. Not
  recommended if the API app and events listener (or multiple API worker
  processes) need concurrent write access at any real volume.
- **Postgres/MySQL/etc.** — any URL SQLAlchemy's `create_engine` accepts
  works unmodified; celery-manager doesn't use any SQLite-specific SQL
  beyond the connection-pooling adjustments described above. Recommended
  for anything beyond local development, and required if you're running
  more than one API process behind a load balancer.

## `cookie_secure` and why it defaults to `True`

The session cookie is a bearer credential: anyone who obtains it *is* the
signed-in user for the remainder of that session, with full access to every
control action the dashboard exposes (start, cancel/terminate, retry, pause a
schedule occurrence). Without the `Secure` flag, a browser will happily send
that cookie over plain HTTP, where anything on the network path can read it.

celery-manager therefore sets `Secure` by default. This is a deliberate
"safe unless you say otherwise" choice: a default of `False` would silently
ship every deployment that never thought about the setting with a leakable
credential, and nothing about the running system would look wrong.

**Set `cookie_secure=False` only for local HTTP development.** Its failure
mode is loud rather than dangerous, which is the point: over plain HTTP with
the default left on, you will log in successfully and then immediately appear
logged out, because the browser accepts the `Secure` cookie but refuses to
send it back over an insecure connection. If that is what you are seeing on
`http://localhost`, this setting is the cause:

```python
config = CeleryManagerConfig(
    celery_app=your_celery_app,
    secret_key="dev-only",
    cookie_secure=False,  # local plain-HTTP development only
)
```

The bundled Django demo (`examples/django_demo`) does exactly this — it runs
over `http://localhost:8001` — via `CELERY_MANAGER_COOKIE_SECURE = False` in
its `settings.py`. Do not carry that line into a real deployment.

Terminating TLS at a proxy in front of celery-manager still counts as HTTPS
from the browser's point of view, so keep the default on in that setup; the
flag describes the browser-to-edge connection, not the edge-to-app hop.

## Schema and upgrades

celery-manager's tables are versioned with [Alembic](https://alembic.sqlalchemy.org/).
The migrations ship inside the package (`celery_manager/db/migrations/`) and
are configured in code, so you don't need an `alembic.ini` or the `alembic`
command.

### Automatic upgrades (the default)

`init_db()`, which the API app, `celery_manager.setup()` in workers and the
`celery-manager events` listener all call, upgrades the database to the latest
revision at startup. Upgrading celery-manager is just: install the new
version, restart. That works from any starting point:

| Database | What happens |
|---|---|
| Empty | Every revision runs, creating all tables. |
| Created by 0.1.0a1–0.1.0a3 (no version table) | Adopted: the baseline revision finds the tables already there and only records the version, then later revisions run. Your data is kept. |
| Created by a pre-release 0.1.0a4 build that already added `paused_at`/`paused_by` itself | Recorded at the revision that adds those columns, so they aren't added twice. Then any later revisions run. |
| Already versioned | Pending revisions run. Nothing happens if it's current. |

Several processes often start at once. The upgrade holds a lock while it
checks and migrates (a PostgreSQL advisory lock, MySQL `GET_LOCK`, or SQLite's
write lock via `BEGIN IMMEDIATE`), so the first process does the work and the
others wait for it, then find nothing to do.

The version is stored in a table called `celery_manager_alembic_version`, not
Alembic's default `alembic_version`. That way celery-manager can share a
database with an application that runs its own Alembic migrations.

The database user needs DDL permissions (`CREATE TABLE`, `ALTER TABLE`,
`CREATE INDEX`) whenever a release includes a new revision. Release notes call
out every schema change. Back up the database before upgrading if its history
matters to you.

### Migrating as a deploy step

If your deploys run migrations separately, or the app's database user
shouldn't have DDL rights, turn automatic upgrades off in every process:

```python
config = CeleryManagerConfig(celery_app=app, ..., auto_migrate=False)
```

Then migrate with the CLI, using a database URL that has DDL rights:

```bash
celery-manager migrate --db-url postgresql://admin@db/celery_manager          # upgrade to latest
celery-manager migrate --db-url postgresql://admin@db/celery_manager --check  # report only
```

`--check` prints the current and latest revision and exits with status 1 if an
upgrade is needed, which makes it usable as a deploy gate. With
`auto_migrate=False`, `init_db()` doesn't touch the database at all. If the
schema isn't current it logs a warning, and queries against the new columns or
tables fail until you run `migrate`.

From Python, the same operations are
`celery_manager.db.migrate.upgrade_database(engine)` and
`migration_status(engine)`.

### Downgrades

Each revision has a `downgrade()`, but celery-manager doesn't wrap downgrades
in a command, and running an older celery-manager against a newer schema
isn't supported. To roll back, restore the backup you took before upgrading.

## `secret_key` in production

Generate a long random value once:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

and set it via your normal secrets-management path (environment variable, a
secrets manager, etc.) — never commit a real value to source control. Every
API process behind the same load balancer needs the *same* key, or a cookie
signed by one is rejected by the next. There is no signing key for the
events listener, which doesn't serve HTTP; its CLI takes no `--secret-key`.

## Security model

Know what celery-manager does and doesn't defend against before you expose
it:

- **Operators are powerful.** Accounts have a [role](roles.md): `viewer`
  (read-only), `operator` or `admin`. Operators can dispatch registered user
  tasks subject to `startable_tasks` and optional strict argument schemas,
  cancel or terminate known tasks, retry runs and pause schedules. Queue
  overrides are disabled by default. Forced cancellation of an unknown ID
  requires an admin and explicit `force=true`. Give `operator` and `admin` only to people you'd
  trust with that, use `viewer` for everyone else, and treat the dashboard
  as an admin surface, ideally reachable only from a private network or VPN.
  Viewers can see task arguments and tracebacks; `log_view_role` can restrict logs.
- **Upgrading makes every existing account `admin`**, because before roles
  every account could do everything. Demote accounts afterwards with
  `celery-manager set-role`.
- **With `authenticate_request`, your app's session is a dashboard
  credential.** Whoever is signed in to your app gets the role your
  `role_for` gives them, so map roles deliberately (the Django default admits
  staff only) and treat an XSS hole anywhere on the same host as a dashboard
  hole. The `Origin` check below still blocks cross-site writes made with your
  app's cookie, whatever its `SameSite` setting; see
  [Security notes](integrations.md#security-notes).
- **Local password login has shared database backoff.** Account and direct-peer
  budgets apply across API instances sharing the database and secret. Configure
  `login_backoff_threshold`, `login_backoff_max_seconds` and
  `login_backoff_reset_seconds`; refused attempts return 429 with `Retry-After`.
  Proxy peer identity must be configured deliberately. External logins retain
  the host's controls. See [login backoff](auth-sessions.md#shared-login-backoff).
- **New browser sessions are individually revocable, valid for 7 days by default.**
  `session_max_age_seconds` controls local-cookie lifetime. HTTP login and bridge
  issuance register a random session ID in the database; logout revokes it before
  removing the cookie. Users can list and revoke their own sessions through the
  API. Live local-role lookup and password fingerprints still apply. Cookies
  issued before this upgrade, or signed manually without a session ID, remain
  stateless until expiry; logout cannot revoke a stolen copy of those older
  cookies. Rotate `secret_key` to invalidate all cookies, or reset a local
  account's password to invalidate its old cookies. See
  [session upgrade compatibility](auth-sessions.md#upgrade-compatibility).
- **`brand_logo_url` may point at an external host.** It lands in an
  `<img src>` on every page, including the login screen, so whoever controls
  that host sees a request (IP address, user agent) from each visitor, and
  can change the image. Prefer a same-origin path (`/logo.png`) or a
  raster `data:` URI; otherwise use a host you control.

What *is* in place: `HttpOnly`, `SameSite=lax`, `Secure`-by-default session
cookies; refusal of cross-origin `POST`/`PUT`/`PATCH`/`DELETE` requests by
`Origin` check (with explicit `trusted_origins` and a bearer-only exception);
`X-Frame-Options: DENY` and `frame-ancestors 'none'` by default against
clickjacking; bcrypt password hashes, with login timing that doesn't reveal
whether a username exists. See the [REST API auth notes](rest-api.md#auth)
for the exact behaviour, including what a proxy in front of the app must
forward.


## Production options introduced in 0.2

The [production adoption guide](production.md) gives working examples, migration
instructions, adapter installation and operational limitations. These options
compose with the existing configuration and retention UI.

| Option | Default | Meaning |
|---|---|---|
| `startable_tasks` | `None` | Registered user tasks only. A collection restricts names; an empty collection disables dispatch. A callable receives `(name, principal)`. |
| `allow_queue_override` | `False` | Permit explicit task-start queue overrides. |
| `retry_edit_args` | `True` | Permit changed arguments when retrying a recorded run. |
| `args_schemas` | `{}` | Task-name to Pydantic model; strict validation of bound arguments before publishing. |
| `log_capture` / `log_capture_level` | `True` / `0` | Capture emitted records and filter by logging level; host logger levels remain authoritative. |
| `log_capture_max_bytes` | `256_000` | UTF-8 head/tail storage limit, including truncation marker. |
| `log_capture_loggers` | `()` | Optional logger-name prefixes; empty accepts all emitted task-context records. |
| `log_redactor` | `None` | Callable transforming a formatted log record. Failure withholds the raw record. |
| `log_view_role` | `"viewer"` | Minimum role that can retrieve captured logs. |
| `store_results` / `result_max_bytes` | `True` / `64_000` | Store bounded result representations in decorator and listener. |
| `principal_providers` | `()` | Ordered external providers, before the existing host callable and local-cookie fallback. |
| `session_max_age_seconds` | `604800` | Local-cookie lifetime; existing password fingerprints and live role lookup still apply. |
| `session_bridge_enabled` | `False` | Opt into a short external-authenticated dashboard cookie. |
| `bridge_max_age_seconds` | `300` | Bridge lifetime; fresh external authentication is required to renew. |
| `trusted_origins` | `()` | Explicit HTTP(S) origins for credentialed CORS and cross-origin cookie writes. |
| `frame_ancestors` | `()` | Explicit embedding origins. Empty denies framing; nonempty replaces deny-frame headers with the configured CSP. |
| `live_stream` | `"sse"` | `"poll"` omits the stream endpoint and disables EventSource in the SPA; use under WSGI. |
| `scheduler_backend` | `None` | Default static discovery, or a backend adapter such as RedBeat/Django Celery Beat. |
| `schedule_sync_interval_seconds` | `30.0` | Minimum interval between request-driven backend snapshot refreshes. |
| `cron_parser` | `None` | Deterministic rules by default, or a host parser/optional `LLMParser`. |
| `db_schema` | `None` | Provisioned SQL schema used by runtime models and migrations. SQLite uses a separate database file. |
| `engine_options` | `{}` | Additional SQLAlchemy engine options. |

Set worker, listener and API options consistently, particularly result/log
collection, storage namespace and scheduler configuration. The beat process uses
`app.conf.celery_manager_db_url` and `celery_manager_db_schema` when recording
optional liveness or reading package timing edits. Retention remains configured
through its existing persistent rules; it is not replaced by a new config flag.


## Operations options introduced in 0.3

| Option | Default | Meaning |
|---|---|---|
| `tenant_resolver` | `None` | Trusted sync/async `(request, principal)` callable returning a tenant ID. Opts HTTP access into fail-closed, read-only task-history isolation; all global operational surfaces and writes are denied, including for admin. See [tenant scope](tenancy.md). |
| `lock_provider` | `None` | Explicit cross-process `LockProvider` for singleton execution. Redis brokers infer a Redis provider; other brokers require an explicit provider. There is no process-local production fallback. |
| `login_backoff_threshold` | `5` | Positive integer: initially admitted local login attempts before exponential backoff begins. Shared account and direct-peer budgets apply. |
| `login_backoff_max_seconds` | `300` | Positive integer: maximum local login backoff delay. |
| `login_backoff_reset_seconds` | `3600` | Positive integer: inactivity period after which a login budget resets. |
| `worker_heartbeat_retention_days` | `7` | Integer 0–3650: retain minute-bucket worker observations for this many days. `0` disables collection and automatic history pruning, preserving existing samples and current WorkerState updates. |

The heartbeat history option supplements task-run retention rules; it does not
replace them. Keep it consistent in listener and API/maintenance processes. See
[worker history](workers.md#recorded-heartbeat-history-03),
[browser sessions](auth-sessions.md) and [singleton execution](singleton.md).
Use `scheduler_backend=SQLAlchemyBeatBackend(app)` for optional durable SQL
definitions; initialize the scheduler's own tables explicitly as documented in
[SQL schedules](sql-schedules.md).

Beat-process settings are Celery app configuration, **not**
`CeleryManagerConfig` constructor arguments:

| Celery app setting | Default | Meaning |
|---|---|---|
| `celery_manager_db_url` | unset | Monitoring database for tick observations and package timing edits; not the optional scheduler definition database. |
| `celery_manager_db_schema` | unset | Provisioned monitoring schema. |
| `celery_manager_heartbeat_interval` | `30` | Tick observation interval, bounded to 5–86400 seconds. |
| `cm_beat_owner_id` | derived identity | Stable logical owner ID; set explicitly for independent production schedulers. |
| `cm_beat_healthcheck_url` | unset | Opt-in HTTP(S) dead-man monitor URL. No embedded credentials or redirects; paths/query tokens are withheld from logs. |
| `cm_beat_healthcheck_interval` | `30` | External ping interval, bounded to 5–86400 seconds. |
| `cm_beat_healthcheck_timeout` | `3` | Socket timeout, bounded to 0.1–10 seconds. DNS may take longer; single-flight requests bound thread growth. |

See [beat liveness](beat-liveness.md) for failure semantics, owner retirement and
external service configuration. A tick ping establishes scheduler liveness,
not task success or leader election.


## Native observation and operations options

See [real-world operations](real-world-operations.md) for working host-runner,
probe, scheduler and preflight examples. These features remain in the same 0.3
release; they do not require converting a host executor to Celery.

| Option | Default | Contract |
|---|---|---|
| `observer_fail_open` | `True` | Persistence failures report telemetry health without failing or retrying host work. A `RunObserver(..., fail_open=...)` can override this. Input validation still rejects invalid instrumentation. |
| `observer_context_max_bytes` | `16384` | Integer of at least 64; oversized sanitized metadata is replaced by an explicit truncation object. Also bounds publish-hook metadata. |
| `observer_metadata_keys` | `("integration", "request_id", "trace_id", "environment", "deployment_id", "resource_id")` | Metadata allowlist; a custom tuple restricts keys, `None` admits all keys before sanitation and limits. Identifiers such as tenant/correlation/operation remain separate fields. |
| `observer_error_callback` | `None` | Callable receiving sanitized observer health after telemetry failure. Callback errors are contained. |
| `observer_store_results` | `False` | Opt into sanitized, bounded results for external observations. Uses the existing result size policy; no automatic argument capture. |
| `environment_name` | `""` | Printable public deployment label, at most 100 characters. Namespaces operation annotations and labels the UI/action audit. It is not a tenant boundary. |
| `require_typed_confirmation` | `False` | HTTP purge must send `confirm` equal to the queue name, and bulk retry `confirm_count` equal to the number of ids. Clients sending either value are always checked; this makes them mandatory. The dashboard always sends them. |
| `operator_safeguards` | `False` | Require an audited reason and exact environment confirmation for the documented sensitive HTTP actions. Requires a nonblank `environment_name`. Role/task policies still apply. |
| `health_probes` | `()` | Up to 32 unique `ProbeDefinition(name, provider)` objects. Providers run only during explicit host collection, never on a health GET. |
| `probe_timeout_seconds` | `3.0` | Finite 0.01–30 seconds; bound collection waiting. A hung synchronous provider remains single-flight rather than being forcibly killed. |
| `probe_freshness_seconds` | `120.0` | Finite 1–86400 seconds; older probe observations become unknown on reads. Alert rules can set their own freshness threshold. |
| `health_observation_retention_days` | `7` | Integer 1–3650; explicit collection removes expired latest probe snapshots in bounded batches. This is not a metrics time series. |

`tenant_resolver` is documented above and in [tenant security](tenancy.md).
Configure it at application construction; do not toggle it per request. Runtime
monitoring writes remain global, while HTTP reads are request-scoped. Provision
and migrate monitoring storage separately when using `auto_migrate=False`.
