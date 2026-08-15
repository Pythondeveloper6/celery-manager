# celery-manager Django demo

A minimal Django project that wires up `celery-manager` against two demo
Celery tasks: `send_welcome_email` (triggered on demand) and `send_report`
(runs on a 2-hour periodic schedule and fails about 30% of the time, so you
can see failure tracebacks and log capture in the dashboard).

## Full setup (Redis + worker + beat + events listener + dashboard)

This is the real-world flow — five processes running side by side, exactly
as you'd run celery-manager against a production Django app.

1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. Start Redis locally (e.g. `redis-server`).
4. `python manage.py migrate`
5. In separate terminals:
   - `python manage.py runserver` (the Django app)
   - `celery -A demo worker -l info` (the worker)
   - `celery -A demo beat -l info` (the periodic scheduler)
   - `celery-manager events --celery-app demo.celery:app --db-url sqlite:///celery_manager_demo.db` (the events listener)
   - `python run_dashboard.py` (the celery-manager dashboard, at http://localhost:8001)
6. Create a dashboard user:
   ```bash
   python -c "
   from celery_manager.auth.users import create_user
   from celery_manager.db.session import init_db, get_session_factory
   from celery_manager.config import CeleryManagerConfig
   from demo.celery import app

   cfg = CeleryManagerConfig(celery_app=app, db_url='sqlite:///celery_manager_demo.db', secret_key='demo-secret-change-me')
   Session = get_session_factory(init_db(cfg))
   create_user(Session(), 'admin', 'admin')
   "
   ```
7. Visit http://localhost:8001 and sign in as `admin` / `admin`. The task
   list is empty until something runs, so trigger `send_welcome_email` —
   see [Triggering a task](#triggering-a-task) below — and watch it appear
   in the task list as the worker processes it.
8. Watch `send_report` fail ~30% of the time on its 2-hour schedule — a
   failed run shows its traceback and captured log output in the task detail
   view. Pause an upcoming `send-demo-report-every-2-hours` occurrence from
   **Schedules → send-demo-report-every-2-hours** to see that occurrence
   recorded as `skipped` instead of executed.

## Triggering a task

There is **no "start task" button in the dashboard UI** — starting a task is
a REST endpoint (`POST /api/tasks/start`), and building a UI for it is a
tracked backlog item, not a v1 feature. Use either of these instead.

**From a Django shell** (simplest, and the same path your own code would
take):

```bash
python manage.py shell -c "
from tasks_app.tasks import send_welcome_email
send_welcome_email.delay('demo@example.com')
"
```

**Or through the REST API**, which is what a UI control would call. The
endpoint requires authentication, so log in first and keep the session cookie
in a jar:

```bash
# 1. Log in, saving the session cookie to ./cm-cookies.txt
curl -sS -c cm-cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"username": "admin", "password": "admin"}' \
  http://localhost:8001/api/auth/login

# 2. Start the task, sending that cookie back
curl -sS -b cm-cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"task_name": "tasks_app.tasks.send_welcome_email", "args": ["demo@example.com"], "kwargs": {}}' \
  http://localhost:8001/api/tasks/start
# -> {"task_id":"...."}
```

The same thing in Python, if you'd rather not juggle cookie jars:

```python
import httpx

with httpx.Client(base_url="http://localhost:8001") as client:
    client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    response = client.post("/api/tasks/start", json={
        "task_name": "tasks_app.tasks.send_welcome_email",
        "args": ["demo@example.com"],
        "kwargs": {},
    })
    print(response.json())  # {'task_id': '...'}
```

Either way the new run shows up in the dashboard's task list, and the
REST-API route additionally records an entry in the **Audit Log** screen
attributing the start to `admin`.

> The `curl`/`httpx` flows work over plain HTTP because this demo sets
> `CELERY_MANAGER_COOKIE_SECURE = False` in `settings.py`. With
> celery-manager's default (`cookie_secure=True`) the session cookie is
> marked `Secure` and a client will not send it back over HTTP.

### What each process is for

- **`manage.py runserver`** — the Django app itself. In this demo it only
  serves a placeholder page; celery-manager's dashboard is a *separate*
  ASGI app (see "Why a separate dashboard process" below), so this process
  isn't required for the dashboard to work, but a real Django app usually
  has other things worth running it for.
- **`celery -A demo worker`** — executes tasks pulled off the broker. Without
  this running, tasks stay queued in Redis and never actually run. This is
  also the process that must be wired with `celery_manager.setup(config)` —
  `tasks_app/tasks.py` does that at import time, which is what makes the
  `@celery_manager.track()` decorators below it actually do their job
  (per-occurrence pause/skip and log capture). Wiring only the dashboard
  process is the classic mistake: tasks run *there* only in eager mode.
- **`celery -A demo beat`** — the periodic scheduler; it's what enqueues
  `send_report` every 2 hours per `CELERY_BEAT_SCHEDULE`. Without beat,
  the periodic schedule never fires (you can still trigger `send_report`
  on demand — see [Triggering a task](#triggering-a-task)).
- **`celery-manager events`** — a long-running consumer of Celery's event
  bus (task-sent/started/succeeded/failed/etc.). It is what records every
  task, decorated or not, and the "queued" -> "started" transitions. Without
  it running, the `@celery_manager.track()` decorator still records each
  run's final outcome directly (since it wraps the task body itself), but
  you lose those intermediate states and any task that isn't decorated.
  Note the dashboard's task list refreshes by polling every few seconds —
  the SSE stream does not yet deliver across process boundaries, see the
  docs' [REST API: live updates](../../docs/rest-api.md#live-updates-sse).
- **`run_dashboard.py`** — the celery-manager REST API + dashboard SPA,
  served by uvicorn on port 8001.

## Why a separate dashboard process

celery-manager's dashboard is a framework-agnostic ASGI app (see
`celery_manager.api.app.create_app`) — it is *not* served through Django's
own WSGI `urlpatterns`. `demo/urls.py` builds the same `create_app(...)`
instance (to prove it constructs cleanly against Django's settings and
Django's request/response cycle can coexist with it in the same process),
but doesn't mount it: WSGI can't serve an ASGI app in-process without an
adapter. The supported v1 story is running celery-manager as its own
`uvicorn` process (`run_dashboard.py`) alongside `manage.py runserver`, as
shown above. Mounting the ASGI dashboard directly inside a Django project
that has switched to serving itself over ASGI (e.g. via Django's own ASGI
support, or an ASGI-in-WSGI adapter) is a documented Phase 2 nicety, not a
v1 requirement.

## Trying task logic without Redis (eager mode)

If you just want to see the task bodies + `@track()` decorator run without
standing up Redis/a worker/beat, set `task_always_eager = True` on the
Celery app (or `CELERY_TASK_ALWAYS_EAGER = True` in settings) and call the
tasks directly/via `.delay()` from a shell:

```bash
python manage.py shell -c "
from tasks_app.tasks import send_welcome_email
send_welcome_email.apply(args=('demo@example.com',))
"
```

This exercises the same `@track()` wiring that records `TaskRun` rows in
`celery_manager_demo.db` — you can then start `run_dashboard.py` and see
the recorded run in the dashboard, without ever needing Redis, a worker,
or beat. (You will not see the live "queued"/"started" streaming that
comes from the `celery-manager events` listener, since that listener
consumes real Celery worker events over the broker.)
