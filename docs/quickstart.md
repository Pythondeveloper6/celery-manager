# Quickstart

## Install

```bash
pip install celery-manager
```

## Wire it into your app

```python
from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app

config = CeleryManagerConfig(
    celery_app=your_celery_app,
    db_url="sqlite:///celery_manager.db",  # or any SQLAlchemy URL
    secret_key="a-real-secret-in-production",
)
app, _ = create_app(config)
```

`create_app` returns `(app, session_factory)`. The FastAPI `app` serves the
JSON API under `/api/*` and — once the frontend has been built and copied
into `src/celery_manager/dashboard/static/` — the dashboard SPA at `/`.
`create_app` also runs a one-time sync of `celery_app.conf.beat_schedule`
into the `periodic_schedules` table on startup, so the dashboard's schedule
list reflects whatever periodic tasks your app has configured.

Run it with any ASGI server, e.g. `uvicorn your_module:app`.

## Run the events listener

```bash
celery-manager events --celery-app your_module:your_celery_app --db-url sqlite:///celery_manager.db
```

This is a separate long-running process (alongside your worker and beat) —
it's what gives the dashboard visibility into every task, decorated or not.
It subscribes to Celery's event bus directly (no broker polling) and
upserts each task's status (`queued`, `started`, `succeeded`, `failed`,
`retried`, `revoked`) into the same database the API reads from.

`--db-url` must point at the same database as the one you pass to
`CeleryManagerConfig` in the app process. `--secret-key` is accepted for
symmetry with `CeleryManagerConfig` but isn't used by the listener itself
(it doesn't serve HTTP or sign sessions); the default is fine to leave
as-is here.

## Create your first dashboard user

```python
from celery_manager.auth.users import create_user
from celery_manager.db.session import init_db, get_session_factory

Session = get_session_factory(init_db(config))
create_user(Session(), "admin", "a-real-password")
```

Passwords are hashed with bcrypt before being stored; there's no
plaintext-password path anywhere in the auth code.

Then sign in at the dashboard URL.

## Opt a task into full tracking (optional)

Every task shows up in the dashboard automatically once the events listener
is running — no code changes required. If you also want per-occurrence
schedule control (pause/cancel one future run of a periodic task) and full
stdout/log capture on the task's detail page, apply `@track` above
`@app.task` — **and wire the worker process with `celery_manager.setup()`**:

```python
# myproject/tasks.py — imported by the Celery worker
import celery_manager
from celery_manager.config import CeleryManagerConfig

from myproject.celery import app

# Required: this runs in the WORKER process, which is where tracked tasks
# execute. create_app() wires the dashboard process only — without this call
# the worker's @track decorators silently do nothing.
celery_manager.setup(CeleryManagerConfig(
    celery_app=app,
    db_url="sqlite:///celery_manager.db",  # the same DB the dashboard uses
    secret_key="a-real-secret-in-production",
))


@app.task(bind=True)
@celery_manager.track(schedule_name="send-report-every-2-hours")
def send_report(self):
    ...
```

`setup()` is idempotent, so it's safe for several task modules to call it.
See [The `@track` decorator](decorator.md) for details.
