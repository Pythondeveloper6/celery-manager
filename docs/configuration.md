# Configuration reference

Every celery-manager process — the API app and the `celery-manager events`
listener — is configured through a single dataclass,
`celery_manager.config.CeleryManagerConfig`:

```python
from celery_manager.config import CeleryManagerConfig

config = CeleryManagerConfig(
    celery_app=your_celery_app,
    db_url="sqlite:///celery_manager.db",
    secret_key="change-me",
    eta_tolerance_seconds=60,
    cookie_secure=True,
)
```

| Field | Type | Default | What it affects |
|---|---|---|---|
| `celery_app` | `celery.Celery` | *required* | Your application's Celery instance. Used for task introspection (dispatching tasks via `send_task`, reading `beat_schedule` to compute upcoming occurrences) and control calls (`revoke`/`terminate`). The API app and the events listener must both be given the *same* Celery app configuration (same broker, same `beat_schedule`) or their views of the world will disagree. |
| `db_url` | `str` | `"sqlite:///celery_manager.db"` | SQLAlchemy connection URL for celery-manager's own storage (task runs, schedules, overrides, users, audit log — see `celery_manager.db.models`). Any SQLAlchemy-supported backend works; swap to Postgres/MySQL/etc. for production. For `sqlite:///:memory:` specifically, the engine is built with `StaticPool` so every connection shares one in-memory database — needed for it to behave sensibly under FastAPI's threadpool (as used in the test suite); other SQLite URLs use `check_same_thread=False` instead. All every process that needs to see the same data (API app + events listener) must point at the same `db_url`. |
| `secret_key` | `str` | `"change-me"` | Signs session cookies for the built-in cookie-based auth (`itsdangerous.URLSafeTimedSerializer`, sessions valid for 7 days). **Must** be overridden to a real, private secret in any deployment other than local development — anyone who knows the key can forge a valid session cookie for any username. Rotating it invalidates every existing session (users are signed out). |
| `eta_tolerance_seconds` | `int` | `60` | How close a stored `ScheduleOverride.target_time` must be to "now" (in the `@track` decorator, at task-run time) or to a freshly recomputed occurrence timestamp (in `GET /api/schedules/{name}/occurrences`, at read time) for them to be treated as the same logical occurrence. See the trade-off below. |
| `cookie_secure` | `bool` | `True` | Sets the `Secure` flag on the session cookie, so browsers only ever send it back over HTTPS. See below for why the default is `True` and when to change it. |

Those five fields are the whole dataclass — `CeleryManagerConfig` has no
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
- **Upgrading across a schema change may require manual work**: either
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

Set it via your normal secrets-management path (environment variable, a
secrets manager, etc.) — never commit a real value to source control. There
is no separate signing key for the events listener; the `--secret-key` CLI
flag exists only for `CeleryManagerConfig` symmetry and isn't used to sign
or verify anything, since the listener doesn't serve HTTP.
