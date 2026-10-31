"""`celery-manager demo`: a populated dashboard in one command.

No broker, worker or database server: a SQLite file seeded with a week of
realistic history (successes, grouped failures with tracebacks, retries,
skipped occurrences, a slow task), periodic schedules on an in-memory Celery
app, and a throwaway admin account. Everything lives in one directory that
the command prints, so it can be deleted afterwards.

Nothing here touches a real broker: dashboard actions that need one (retry,
revoke, purge) answer with the broker's error, which is itself a useful
thing to see before deploying.
"""
from __future__ import annotations

import json
import random
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEMO_USER = "demo"

_TASKS = {
    # name: (queue, typical seconds, failure rate)
    "billing.charge_invoice": ("billing", 1.2, 0.08),
    "reports.build_daily": ("reports", 42.0, 0.03),
    "emails.send_digest": ("default", 0.4, 0.01),
    "imports.sync_catalog": ("imports", 18.0, 0.15),
    "cache.refresh": ("default", 0.2, 0.0),
}

_FAILURES = {
    "billing.charge_invoice": ("PaymentGatewayTimeout", "gateway did not answer within 10s"),
    "reports.build_daily": ("MemoryError", "worker exceeded memory limit"),
    "emails.send_digest": ("SMTPServerDisconnected", "Connection unexpectedly closed"),
    "imports.sync_catalog": ("HTTPError", "502 Bad Gateway from supplier API"),
    "cache.refresh": ("ConnectionError", "Redis connection refused"),
}


@dataclass
class DemoSite:
    directory: Path
    db_url: str
    password: str
    runs: int


def demo_celery_app():
    """An in-memory Celery app with the demo's task names and schedules."""
    from celery import Celery
    from celery.schedules import crontab

    app = Celery("celery_manager_demo", broker="memory://", backend="cache+memory://")
    app.conf.update(timezone="UTC", task_always_eager=False)
    for name in _TASKS:
        app.task(name=name)(lambda *args, **kwargs: None)
    app.conf.beat_schedule = {
        "daily-report": {"task": "reports.build_daily", "schedule": crontab(minute="0", hour="6")},
        "catalog-sync": {"task": "imports.sync_catalog", "schedule": crontab(minute="*/30")},
        "cache-refresh": {"task": "cache.refresh", "schedule": timedelta(minutes=5)},
        "nightly-digest": {"task": "emails.send_digest", "schedule": crontab(minute="30", hour="22")},
    }
    return app


def _traceback(name: str, kind: str, message: str) -> str:
    module = name.split(".")[0]
    return (
        "Traceback (most recent call last):\n"
        f'  File "/app/{module}/tasks.py", line 42, in {name.split(".")[-1]}\n'
        "    result = client.call(payload)\n"
        f'  File "/app/{module}/client.py", line 88, in call\n'
        "    raise " + kind + "(message)\n"
        f"{kind}: {message}\n"
    )


def seed(session, *, now: datetime | None = None, days: int = 7, rng: random.Random | None = None) -> int:
    """Insert demo history into an initialized celery-manager database.
    Returns the number of task runs written. Deterministic for a given rng."""
    from celery_manager.db.models import TaskRun

    rng = rng or random.Random(7)
    now = now or datetime.now(timezone.utc)
    rows = []
    start = now - timedelta(days=days)
    for name, (queue, seconds, failure_rate) in _TASKS.items():
        count = {"cache.refresh": 160, "billing.charge_invoice": 120}.get(name, 40)
        for i in range(count):
            started = start + timedelta(seconds=rng.uniform(0, days * 86400 - 600))
            runtime = max(0.05, rng.gauss(seconds, seconds * 0.25))
            if name == "reports.build_daily" and i % 9 == 0:
                runtime *= 3  # a few slow runs so the duration charts have a tail
            failed = rng.random() < failure_rate
            kind, message = _FAILURES[name]
            rows.append(TaskRun(
                task_id=str(uuid.UUID(int=rng.getrandbits(128))),
                task_name=name,
                status="failed" if failed else "succeeded",
                queue=queue,
                worker=f"celery@demo-worker-{rng.randint(1, 3)}",
                args=json.dumps([rng.randint(1000, 9999)]),
                kwargs="{}",
                sent_at=started - timedelta(seconds=rng.uniform(0.01, 2)),
                received_at=started - timedelta(seconds=0.01),
                started_at=started,
                finished_at=started + timedelta(seconds=runtime),
                runtime_seconds=round(runtime, 3),
                exception=f"{kind}('{message}')" if failed else None,
                traceback=_traceback(name, kind, message) if failed else None,
                result_repr=None if failed else json.dumps({"ok": True}),
                retry_count=rng.choice([0, 0, 0, 1]) if failed else 0,
            ))
    # Skipped scheduled occurrences, as an operator pause would leave them.
    for day in range(1, 3):
        at = (now - timedelta(days=day)).replace(hour=6, minute=0, second=0, microsecond=0)
        rows.append(TaskRun(task_id=f"demo-skip-{day}", task_name="reports.build_daily", status="skipped",
                            schedule_name="daily-report", occurrence_at=at, skip_reason="schedule paused",
                            queue="reports", args="[]", kwargs="{}", started_at=at, finished_at=at))
    # One running task with live progress.
    rows.append(TaskRun(task_id="demo-running", task_name="imports.sync_catalog", status="started", queue="imports",
                        worker="celery@demo-worker-1", args="[]", kwargs="{}", started_at=now - timedelta(minutes=3),
                        progress_current=640, progress_total=1000, progress_message="Importing products",
                        progress_updated_at=now))
    session.add_all(rows)
    session.commit()
    return len(rows)


def build(directory: Path | None = None, *, rng: random.Random | None = None) -> DemoSite:
    """Create the demo database, user and history. Returns where it lives."""
    import tempfile

    from celery_manager.auth.users import create_user
    from celery_manager.config import CeleryManagerConfig
    from celery_manager.db.session import get_session_factory, init_db

    directory = Path(directory or tempfile.mkdtemp(prefix="celery-manager-demo-"))
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / "demo.db"
    if db_path.exists():
        raise FileExistsError(f"{db_path} already exists; choose an empty directory")
    db_url = f"sqlite:///{db_path}"
    engine = init_db(CeleryManagerConfig(celery_app=None, db_url=db_url))
    password = secrets.token_urlsafe(9)
    try:
        with get_session_factory(engine)() as session:
            create_user(session, DEMO_USER, password, role="admin")
            runs = seed(session, rng=rng)
    finally:
        engine.dispose()
    return DemoSite(directory=directory, db_url=db_url, password=password, runs=runs)


def demo_config(site: DemoSite):
    from celery_manager.config import CeleryManagerConfig

    return CeleryManagerConfig(
        celery_app=demo_celery_app(),
        db_url=site.db_url,
        secret_key=secrets.token_hex(32),
        environment_name="demo",
        # Served over plain HTTP on a loopback address, where some browsers
        # drop Secure cookies. A throwaway local demo, never a deployment.
        cookie_secure=False,
    )


def run(directory: Path | None = None, *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    """`celery-manager demo`. Builds the site, prints how to sign in and
    serves the dashboard until interrupted."""
    try:
        import uvicorn
    except ImportError:
        print('error: the demo needs uvicorn: pip install "celery-manager[standalone]"')
        return 1
    from celery_manager.api.app import create_app

    site = build(directory)
    app, _ = create_app(demo_config(site))
    url = f"http://{host}:{port}/"
    print(f"celery-manager demo: {site.runs} runs seeded in {site.directory}")
    print(f"Open {url} and sign in as {DEMO_USER!r} with password {site.password!r}")
    print("Ctrl+C stops it; delete the directory above to remove everything.", flush=True)
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
