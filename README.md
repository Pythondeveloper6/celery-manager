# celery-manager

A dashboard and control API for Celery background tasks — for any
Django, Flask, or FastAPI project that already uses Celery.

Point it at your existing Celery app and you get a web UI and a REST API to
see what your tasks are doing, act on them, and — the feature this project
exists for — **pause or cancel a single future occurrence of a periodic task**
without touching your beat schedule or restarting anything.

## What you get

- **Task visibility** — every task run (decorated or not) with status, args,
  worker, queue, timings, exceptions, and full tracebacks.
- **Full run history** — browse past runs beyond the newest page, search by
  task name, task ID, or args/kwargs content, and filter by status and date
  range.
- **Live progress** — call `celery_manager.report_progress(current, total,
  message)` inside a task body and watch the bar move, with elapsed time and
  a projected finish while the task is still running.
- **Log capture** — `logging` output from inside a task body (and `print()`
  too, in a real worker — [see why](docs/decorator.md#what-log-capture-actually-captures)),
  attached to that run and readable in the dashboard.
- **Control** — start, cancel/terminate, retry, and bulk retry, from the UI
  or the API. Retries keep their lineage, so you can walk the chain.
- **Per-occurrence schedule control** — skip or cancel one upcoming run of a
  periodic task, and see it stay paused.
- **Audit log** — who did what, when, for every control action.
- **Light and dark themes** — an explicit per-user choice, not just whatever
  the OS says.
- **Brandable** — set the product name, accent colour, and logo from a
  settings screen; it applies for everyone on the deployment.
- **Built-in auth** — bcrypt-hashed users and signed session cookies.
- **No Node toolchain required** — the React dashboard ships prebuilt inside
  the Python package.

## Install

```bash
pip install --pre celery-manager

# add the `standalone` extra if you want to run the dashboard with uvicorn
# rather than mounting it into an ASGI app you already serve:
pip install --pre "celery-manager[standalone]"
```

## Wire it up

celery-manager runs in two places, and **both** need configuring — this is
the step most easily missed:

```python
from celery_manager.config import CeleryManagerConfig

config = CeleryManagerConfig(
    celery_app=your_celery_app,
    db_url="sqlite:///celery_manager.db",   # any SQLAlchemy URL
    secret_key="a-real-secret-in-production",
)
```

**1. The dashboard process** — build the ASGI app and serve or mount it:

```python
from celery_manager.api.app import create_app

app, _ = create_app(config)   # then: uvicorn your_module:app
```

**2. The Celery worker process** — where `@track`-decorated tasks actually
run:

```python
import celery_manager

celery_manager.setup(config)   # at import time, in your tasks module

@app.task(bind=True)
@celery_manager.track(schedule_name="send-report-every-2-hours")
def send_report(self):
    ...
```

Then run the events listener alongside your worker and beat, so the dashboard
sees every task rather than only the decorated ones:

```bash
celery-manager events --celery-app your_module:your_celery_app \
                      --db-url sqlite:///celery_manager.db
```

**Two things that stop people on a first run**, both covered in the
[Quickstart](docs/quickstart.md): there is no default account, so you have to
create one (`celery_manager.auth.users.create_user`); and on plain HTTP you
need `cookie_secure=False`, or you will log in successfully and then
immediately appear logged out.

## Documentation

Full documentation lives in [`docs/`](docs/) (an MkDocs Material site — run
`mkdocs serve` after `pip install -e ".[docs]"`):

- [Quickstart](docs/quickstart.md) — install through first sign-in.
- [The `@track` decorator](docs/decorator.md) — wiring, skip/cancel, logs.
- [Live progress](docs/progress.md) — reporting progress from inside a task.
- [REST API](docs/rest-api.md) — every endpoint and payload.
- [Dashboard](docs/dashboard.md) — the screens and what they do.
- [Configuration](docs/configuration.md) — every config field, the schema
  and upgrade story, and production notes.
- [Working on celery-manager](docs/contributing.md) — running the tests,
  building the docs, and rebuilding the committed dashboard bundle.

Changes between versions — including the one breaking API change so far —
are in [`CHANGELOG.md`](CHANGELOG.md).

A complete runnable Django example is in
[`examples/django_demo/`](examples/django_demo/).

## Requirements

Python 3.10+, Celery 5.x. Storage defaults to SQLite with zero configuration
and swaps to Postgres/MySQL/anything SQLAlchemy supports via `db_url`.

## Status

This is the first alpha, **0.1.0a1**. It is feature-complete for the v1 scope,
but APIs may change before 0.1.0. See [`plan.md`](plan.md) for the implementation history and the
backlog of known gaps and planned follow-ups.

## License

MIT — see [`LICENSE`](LICENSE).
