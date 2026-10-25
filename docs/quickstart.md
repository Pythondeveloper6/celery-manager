# Quickstart

## Install

celery-manager 0.1.0a1 is an alpha in a **private** GitHub repository and is
**not on PyPI yet** (publication is planned), so `pip install --pre
celery-manager` will not find it. Install the tagged release from git (this
needs access to the repository):

```bash
pip install "git+https://github.com/Pythondeveloper6/celery-manager.git@v0.1.0a1"
```

or from the wheel attached to the GitHub release:

```bash
pip install ./celery_manager-0.1.0a1-py3-none-any.whl
```

Extras go after the name, e.g. `"celery-manager[standalone] @
git+https://github.com/Pythondeveloper6/celery-manager.git@v0.1.0a1"`. Once the
package is published, `pip install --pre celery-manager` will work (`--pre`
because 0.1.0a1 is a pre-release).

`uvicorn` is **not** a core dependency — celery-manager is an ASGI app, and
mounting it into a server you already run needs no server of its own. If you
want to serve the dashboard standalone (`uvicorn your_module:app`, or the
bundled demo's `run_dashboard.py`), install the `standalone` extra
(`celery-manager[standalone]`). To mount it in a Flask app, install the `flask`
extra (`celery-manager[flask]`, which adds Flask and `a2wsgi`) — see
[Integrating with your app](integrations.md#flask-wsgi).

## Wire it into your app

```python
import os

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app

config = CeleryManagerConfig(
    celery_app=your_celery_app,
    db_url="sqlite:///celery_manager.db",  # or any SQLAlchemy URL
    secret_key=os.environ["CELERY_MANAGER_SECRET_KEY"],  # private + random
)
app, _ = create_app(config)
```

`secret_key` signs every session cookie, so it must be private and random —
generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"` and load it
from your secrets store. `create_app` raises `ValueError` and refuses to
start if it is empty or left at the `"change-me"` default.

`create_app` returns `(app, session_factory)`. The FastAPI `app` serves the
JSON API under `/api/*` and — once the frontend has been built and copied
into `src/celery_manager/dashboard/static/` — the dashboard SPA at `/` (or at
whatever prefix you [mount it under](integrations.md); the SPA uses relative
URLs).
`create_app` also runs a one-time sync of `celery_app.conf.beat_schedule`
into the `periodic_schedules` table on startup, so the dashboard's schedule
list reflects whatever periodic tasks your app has configured.

Run it with any ASGI server, e.g. `uvicorn your_module:app` (installing
the `standalone` extra above if you don't already have uvicorn).

!!! warning "Running on plain HTTP? You need `cookie_secure=False`"
    This is the single most likely thing to stop you on your first run. The
    session cookie is `Secure` by default, and a browser will accept a
    `Secure` cookie over plain HTTP but **refuse to send it back** — so you
    log in successfully and then immediately appear logged out, with nothing
    in any log to explain it.

    If you are developing against `http://localhost`, add
    `cookie_secure=False` to the config above. Leave the default alone
    anywhere reachable over anything else — see
    [Configuration](configuration.md#cookie_secure-and-why-it-defaults-to-true).

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
`CeleryManagerConfig` in the app process. The listener takes no secret
key: it doesn't serve HTTP or sign sessions.

### Required Celery settings

The events listener only records what Celery *emits*, and Celery emits task
events only when asked. Set both of these on your Celery app (the publishing
side needs the second one, so put them in the config shared by web, worker and
beat):

```python
app.conf.worker_send_task_events = True   # same as running the worker with -E
app.conf.task_send_sent_event = True      # publisher emits task-sent (carries the queue)
```

Alternatively start the worker with `celery -A yourapp worker -E`, but
`task_send_sent_event` has no CLI flag, so it must be in the config.

Without them:

- No `-E` / `worker_send_task_events`: the listener receives nothing from the
  worker, so `worker` stays `null` and the listener contributes no
  queued/started/finished transitions or runtimes.
- No `task_send_sent_event`: `queue` stays `null` (it comes only from the
  `task-sent` event).
- Tasks decorated with [`@track`](decorator.md) still record status, timings,
  exceptions and logs from the worker process, but never `worker` or `queue`,
  and undecorated tasks are not recorded at all.

!!! warning "macOS: prefork workers"
    On macOS, a prefork worker can fail every task with
    `ValueError: not enough values to unpack (expected 3, got 0)`. The cause is
    an upstream Celery 5.6 / billiard 4.3 bug: billiard spawns pool children
    instead of forking them, and Celery only initialises a spawned child when
    `FORKED_BY_MULTIPROCESSING` is set. This is not caused by celery-manager.
    Workarounds: start the worker with `FORKED_BY_MULTIPROCESSING=1 celery -A yourapp worker ...`,
    or, for local development, use `--pool=threads` or `--pool=solo`.

The listener (`celery-manager events`) must run as its own long-lived process,
next to the worker and beat.

## Create your first dashboard user

```python
from celery_manager.auth.users import create_user
from celery_manager.db.session import init_db, get_session_factory

Session = get_session_factory(init_db(config))
create_user(Session(), "admin", "a-real-password")
```

Passwords are hashed with bcrypt before being stored; there's no
plaintext-password path anywhere in the auth code. A password must be 1 to 72
bytes when UTF-8 encoded (bcrypt's limit): `create_user` raises `ValueError` for
an empty one or a longer one rather than silently truncating it.

There is no default account and no first-run setup screen, so **the
dashboard is unusable until you run this** — it is the second thing that
stops people on a first run. Run it once, in a shell with your `config` in
scope; the users table lives in the same `db_url` as everything else.

Then sign in at the dashboard URL.

## Opt a task into full tracking (optional)

Every task shows up in the dashboard automatically once the events listener
is running — no code changes required. If you also want per-occurrence
schedule control (pause/cancel one future run of a periodic task) and log
capture on the task's detail page, apply `@track` above
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

## Report live progress (optional)

Once the worker is wired, a task can publish its own progress to the
dashboard while it is still running:

```python
from celery_manager import report_progress


@app.task(bind=True)
@celery_manager.track()
def import_customers(self, rows):
    for i, row in enumerate(rows):
        handle(row)
        report_progress(i + 1, len(rows), f"row {i + 1}")
```

Database writes are throttled to one every 0.5 seconds per run, so this is
safe inside a tight loop, and the call never raises into your task. Omit
`total` for open-ended work. See [Live progress](progress.md).
