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
)
```

| Field | Type | Default | What it affects |
|---|---|---|---|
| `celery_app` | `celery.Celery` | *required* | Your application's Celery instance. Used for task introspection (dispatching tasks via `send_task`, reading `beat_schedule` to compute upcoming occurrences) and control calls (`revoke`/`terminate`). The API app and the events listener must both be given the *same* Celery app configuration (same broker, same `beat_schedule`) or their views of the world will disagree. |
| `db_url` | `str` | `"sqlite:///celery_manager.db"` | SQLAlchemy connection URL for celery-manager's own storage (task runs, schedules, overrides, users, audit log — see `celery_manager.db.models`). Any SQLAlchemy-supported backend works; swap to Postgres/MySQL/etc. for production. For `sqlite:///:memory:` specifically, the engine is built with `StaticPool` so every connection shares one in-memory database — needed for it to behave sensibly under FastAPI's threadpool (as used in the test suite); other SQLite URLs use `check_same_thread=False` instead. All every process that needs to see the same data (API app + events listener) must point at the same `db_url`. |
| `secret_key` | `str` | `"change-me"` | Signs session cookies for the built-in cookie-based auth (`itsdangerous.URLSafeTimedSerializer`, sessions valid for 7 days). **Must** be overridden to a real, private secret in any deployment other than local development — anyone who knows the key can forge a valid session cookie for any username. Rotating it invalidates every existing session (users are signed out). |
| `eta_tolerance_seconds` | `int` | `60` | How close a stored `ScheduleOverride.target_time` must be to "now" (in the `@track` decorator, at task-run time) or to a freshly recomputed occurrence timestamp (in `GET /api/schedules/{name}/occurrences`, at read time) for them to be treated as the same logical occurrence. See the trade-off below. |

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

## `secret_key` in production

Set it via your normal secrets-management path (environment variable, a
secrets manager, etc.) — never commit a real value to source control. There
is no separate signing key for the events listener; the `--secret-key` CLI
flag exists only for `CeleryManagerConfig` symmetry and isn't used to sign
or verify anything, since the listener doesn't serve HTTP.
