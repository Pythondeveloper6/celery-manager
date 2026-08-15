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
7. Visit http://localhost:8001, sign in as `admin` / `admin`, trigger
   `send_welcome_email` from the "start task" action, and watch it show up
   live in the task list as the worker processes it. Watch `send_report`
   fail ~30% of the time on its 2-hour schedule — a failed run shows its
   traceback and captured log output in the task detail view. Pause an
   upcoming `send-demo-report-every-2-hours` occurrence from the schedule
   view to see that occurrence recorded as `skipped` instead of executed.

### What each process is for

- **`manage.py runserver`** — the Django app itself. In this demo it only
  serves a placeholder page; celery-manager's dashboard is a *separate*
  ASGI app (see "Why a separate dashboard process" below), so this process
  isn't required for the dashboard to work, but a real Django app usually
  has other things worth running it for.
- **`celery -A demo worker`** — executes tasks pulled off the broker. Without
  this running, tasks stay queued in Redis and never actually run.
- **`celery -A demo beat`** — the periodic scheduler; it's what enqueues
  `send_report` every 2 hours per `CELERY_BEAT_SCHEDULE`. Without beat,
  the periodic schedule never fires (you can still trigger `send_report`
  manually via the dashboard).
- **`celery-manager events`** — a long-running consumer of Celery's event
  bus (task-sent/started/succeeded/failed/etc.), which is how the
  dashboard's task list updates live without polling. Without this
  running, the `@track()` decorator still records each run's outcome
  directly (since it wraps the task body itself), but you lose the
  live "queued" -> "started" state transitions streamed from the worker.
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
