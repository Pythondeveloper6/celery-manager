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
    cookie_secure=True,
    inspect_timeout_seconds=1.0,
)
```

| Field | Type | Default | What it affects |
|---|---|---|---|
| `celery_app` | `celery.Celery` | *required* | Your application's Celery instance. Used for task introspection (dispatching tasks via `send_task`, reading `beat_schedule` to compute upcoming occurrences) and control calls (`revoke`/`terminate`). The API app and the events listener must both be given the *same* Celery app configuration (same broker, same `beat_schedule`) or their views of the world will disagree. |
| `db_url` | `str` | `"sqlite:///celery_manager.db"` | SQLAlchemy connection URL for celery-manager's own storage (task runs, schedules, overrides, users, audit log — see `celery_manager.db.models`). Any SQLAlchemy-supported backend works; swap to Postgres/MySQL/etc. for production. For `sqlite:///:memory:` specifically, the engine is built with `StaticPool` so every connection shares one in-memory database — needed for it to behave sensibly under FastAPI's threadpool (as used in the test suite); other SQLite URLs use `check_same_thread=False` instead. Every process that needs to see the same data (API app + events listener) must point at the same `db_url`. |
| `secret_key` | `str` | `"change-me"` (a placeholder) | Signs session cookies for the built-in cookie-based auth (`itsdangerous.URLSafeTimedSerializer`, sessions valid for 7 days). Anyone who knows the key can forge a valid session cookie for any username, so **`create_app()` raises `ValueError` and refuses to start if it is empty or still `"change-me"`** — set a private random value in every process that serves the dashboard, local development included. Processes that don't serve HTTP (Celery workers calling `celery_manager.setup()`, the events listener) never sign sessions and may leave the default. Rotating it invalidates every existing session (users are signed out). See [`secret_key` in production](#secret_key-in-production). |
| `eta_tolerance_seconds` | `int` | `60` | How close a stored `ScheduleOverride.target_time` must be to "now" (in the `@track` decorator, at task-run time) or to a freshly recomputed occurrence timestamp (in `GET /api/schedules/{name}/occurrences`, at read time) for them to be treated as the same logical occurrence. See the trade-off below. |
| `cookie_secure` | `bool` | `True` | Sets the `Secure` flag on the session cookie, so browsers only ever send it back over HTTPS. See below for why the default is `True` and when to change it. |
| `inspect_timeout_seconds` | `float` | `1.0` | How long the [Workers page](workers.md) and the worker control endpoints wait for workers to reply to a broadcast (`inspect()`, `pool_grow`, `rate_limit`, ...). The HTTP request blocks for it, so keep it short; raise it only if workers on a slow or distant broker show as missing when they are running. |

Those six fields are the whole dataclass — `CeleryManagerConfig` has no
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

- **Too small** (e.g. a few seconds) risks the decorator's check missing a
  match at actual run time if the worker picks up the task even slightly
  outside the window — the task would run instead of being skipped, silently
  defeating the pause.
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

`init_db()` creates every table it needs on first startup via SQLAlchemy's
`Base.metadata.create_all`. That is the whole story for v1:

- **There is no migration framework.** No Alembic, no versioned migration
  history, no `celery-manager migrate` command.
- `create_all` only ever *creates missing tables*. It never alters an
  existing table — so if a future version of celery-manager adds or changes a
  column, starting the new version against an older database will not update
  the schema, and you will get errors about missing columns rather than an
  automatic upgrade.
- **One exception: known nullable columns are added for you.** A short,
  explicit list of nullable columns added to an existing table after it
  first shipped (`_ADDITIVE_COLUMNS` in `celery_manager.db.session`) is
  checked on every `init_db()`, and any that are missing are added with
  `ALTER TABLE ... ADD COLUMN`. Adding a nullable column is safe on every
  backend and rewrites no data. Today that list is
  `periodic_schedules.paused_at` and `periodic_schedules.paused_by` (the
  whole-schedule pause). The database user therefore needs `ALTER`
  permission on that table the first time a new version starts.
- **Any other schema change may require manual work**: either
  hand-writing the DDL (`ALTER TABLE ...`) to match the new models in
  `celery_manager.db.models`, or dropping and recreating celery-manager's
  database. Recreating it is usually acceptable — the tables hold
  observability history (task runs, the audit log), your schedule overrides,
  and dashboard user accounts, none of which your application depends on to
  keep running — but you would lose that history and have to recreate users.
- Release notes will call out any schema change explicitly. Back up the
  database before upgrading if the history matters to you.

Adopting a real migration framework is a Phase 2 backlog item (see
`plan.md`).

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

- **Every dashboard account is a full administrator.** There are no roles
  or scopes. Any signed-in user can dispatch *any* task name with *any*
  args and kwargs (`POST /api/tasks/start` does not check the name against
  registered tasks), cancel or terminate any task, retry runs, pause
  schedule occurrences, and rebrand the dashboard. Only create accounts for
  people you'd trust with that, and treat the dashboard as an admin
  surface — ideally reachable only from a private network or VPN.
- **There is no login rate limiting or lockout.** Put a rate limit on
  `POST /api/auth/login` at your reverse proxy (e.g. nginx `limit_req`,
  Traefik `RateLimit`, a cloud WAF rule). bcrypt makes each guess cost
  ~0.2 s of server CPU, which slows brute force but is not a substitute.
- **Sessions are stateless signed tokens, valid for 7 days.** The
  `cm_session` cookie *is* the session; nothing is stored server-side.
  Logging out deletes the cookie from that browser but does **not** revoke
  a copy someone else already has — a stolen cookie keeps working until it
  expires. Deleting the user does stop their cookie working (every request
  checks the user still exists). To revoke *every* session at once, rotate
  `secret_key`.
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
