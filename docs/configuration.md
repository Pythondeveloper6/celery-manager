# Configuration reference

Every celery-manager process — the API app and the `celery-manager events`
listener — is configured through a single dataclass,
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
)
```

| Field | Type | Default | What it affects |
|---|---|---|---|
| `celery_app` | `celery.Celery` | *required* | Your application's Celery instance. Used for task introspection (dispatching tasks via `send_task`, reading `beat_schedule` to compute upcoming occurrences) and control calls (`revoke`/`terminate`). The API app and the events listener must both be given the *same* Celery app configuration (same broker, same `beat_schedule`) or their views of the world will disagree. |
| `db_url` | `str` | `"sqlite:///celery_manager.db"` | SQLAlchemy connection URL for celery-manager's own storage (task runs, schedules, overrides, users, audit log — see `celery_manager.db.models`). Any SQLAlchemy-supported backend works; swap to Postgres/MySQL/etc. for production. For `sqlite:///:memory:` specifically, the engine is built with `StaticPool` so every connection shares one in-memory database — needed for it to behave sensibly under FastAPI's threadpool (as used in the test suite); other SQLite URLs use `check_same_thread=False` instead. Every process that needs to see the same data (API app + events listener) must point at the same `db_url`. |
| `secret_key` | `str` | `"change-me"` (a placeholder) | Signs session cookies for the built-in cookie-based auth (`itsdangerous.URLSafeTimedSerializer`, sessions valid for 7 days). Anyone who knows the key can forge a valid session cookie for any username, so **`create_app()` raises `ValueError` and refuses to start if it is empty or still `"change-me"`** — set a private random value in every process that serves the dashboard, local development included. Processes that don't serve HTTP (Celery workers calling `celery_manager.setup()`, the events listener) never sign sessions and may leave the default. Rotating it invalidates every existing session (users are signed out). See [`secret_key` in production](#secret_key-in-production). |
| `eta_tolerance_seconds` | `int` | `60` | How far apart two timestamps of the same occurrence may be: an interval-schedule occurrence stamped by beat vs. a saved `target_time` (in `@track`, capped at half the interval), and a saved `target_time` vs. a recomputed occurrence (in `GET /api/schedules/{name}/occurrences`). It is compared with "now" only for messages without occurrence headers while beat has never stamped any (see [How the skip/cancel check works](decorator.md#how-the-skipcancel-check-works)). See the trade-off below. |
| `missed_run_grace_seconds` | `int` | `300` | How long after a scheduled run was due it may still arrive before it is reported as missed (`GET /api/schedules/{name}/missed`, the `missed_count` on `GET /api/schedules`). Detection needs beat to run the stamping scheduler (see [Missed-run detection](decorator.md#missed-run-detection)). Raise it if your broker or workers are routinely slow to start a run. |
| `cookie_secure` | `bool` | `True` | Sets the `Secure` flag on the session cookie, so browsers only ever send it back over HTTPS. See below for why the default is `True` and when to change it. |
| `export_max_rows` | `int` | `50000` | Hard cap on rows in one `GET /api/tasks/export` download. Larger results are cut to the newest rows and flagged with `X-Export-Truncated: true`. |
| `live_poll_interval_seconds` | `float` | `1.0` | How often each API process polls `task_runs` for changes to push down `/api/stream` while at least one dashboard has it open. Each poll is a small query on an indexed column; nothing is polled while nobody is connected. Lower means quicker live updates and more queries. See [REST API: how changes reach the stream](rest-api.md#how-changes-reach-the-stream). |
| `auto_migrate` | `bool` | `True` | Whether `init_db()` upgrades the database schema to the latest revision at startup. Set it to `False` to run `celery-manager migrate` as a deploy step instead; `init_db()` then leaves the database untouched and logs a warning if the schema isn't current. See [Schema and upgrades](#schema-and-upgrades). |

| `inspect_timeout_seconds` | `float` | `1.0` | How long the [Workers page](workers.md) and the worker control endpoints wait for workers to reply to a broadcast (`inspect()`, `pool_grow`, `rate_limit`, ...). The HTTP request blocks for it, so keep it short; raise it only if workers on a slow or distant broker show as missing when they are running. |

Those nine fields are the whole dataclass — `CeleryManagerConfig` has no
other options, and nothing else in celery-manager reads configuration from
the environment.

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
  (read-only), `operator` or `admin`. An `operator` can dispatch *any* task
  name with *any* args and kwargs (`POST /api/tasks/start` does not check
  the name against registered tasks), cancel or terminate any task, retry
  runs and pause schedules. Give `operator` and `admin` only to people you'd
  trust with that, use `viewer` for everyone else, and treat the dashboard
  as an admin surface, ideally reachable only from a private network or VPN.
  Viewers can see task arguments, logs and tracebacks.
- **Upgrading makes every existing account `admin`**, because before roles
  every account could do everything. Demote accounts afterwards with
  `celery-manager set-role`.
- **There is no login rate limiting or lockout.** Put a rate limit on
  `POST /api/auth/login` at your reverse proxy (e.g. nginx `limit_req`,
  Traefik `RateLimit`, a cloud WAF rule). bcrypt makes each guess cost
  ~0.2 s of server CPU, which slows brute force but is not a substitute.
- **Sessions are stateless signed tokens, valid for 7 days.** The
  `cm_session` cookie *is* the session; nothing is stored server-side.
  Logging out deletes the cookie from that browser but does **not** revoke
  a copy someone else already has — a stolen cookie keeps working until it
  expires. Deleting the user, or changing their role, takes effect on their
  next request (every request reads the account and its role from the
  database). Resetting or changing an account's password also ends every
  session it had: the cookie carries a fingerprint of the password hash and
  a mismatch is a 401. See [managing accounts](roles.md#managing-accounts).
  To revoke *every* session at once, rotate `secret_key`.
- **`brand_logo_url` may point at an external host.** It lands in an
  `<img src>` on every page, including the login screen, so whoever controls
  that host sees a request (IP address, user agent) from each visitor, and
  can change the image. Prefer a same-origin path (`/logo.png`) or a
  raster `data:` URI; otherwise use a host you control.

What *is* in place: `HttpOnly`, `SameSite=lax`, `Secure`-by-default session
cookies; refusal of cross-origin `POST`/`PUT`/`PATCH`/`DELETE` requests by
`Origin` check; `X-Frame-Options: DENY` and `frame-ancestors 'none'` against
clickjacking; bcrypt password hashes, with login timing that doesn't reveal
whether a username exists. See the [REST API auth notes](rest-api.md#auth)
for the exact behaviour, including what a proxy in front of the app must
forward.
