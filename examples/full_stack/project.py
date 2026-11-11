"""One module that every process of a complete celery-manager stack imports.

    worker     celery -A project worker --pool=threads -E
    beat       celery -A project beat -S celery_manager.beat:DatabaseScheduler
    events     celery-manager events --config project:config
    dashboard  uvicorn --factory project:make_dashboard

Configuration comes from environment variables so the same file runs on a
laptop and in the end-to-end test (see README.md for the CM_EXAMPLE_* list).
"""
import os

from celery import Celery

import celery_manager
from celery_manager import CeleryManagerConfig

BROKER_URL = os.environ.get("CM_EXAMPLE_BROKER_URL", "redis://127.0.0.1:6379/0")
DB_URL = os.environ.get("CM_EXAMPLE_DB_URL", "sqlite:///full_stack.db")
# Seconds between runs of the demo periodic task (kept short so beat is visible).
TICK_SECONDS = float(os.environ.get("CM_EXAMPLE_TICK_SECONDS", "30"))

app = Celery("project", broker=BROKER_URL)
app.conf.update(
    timezone="UTC",
    # Celery must emit events for the listener (and the dashboard) to see tasks.
    worker_send_task_events=True,
    task_send_sent_event=True,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    # Where beat's database scheduler reads schedule edits from.
    celery_manager_db_url=DB_URL,
    celery_manager_schedule_refresh=2,
    beat_schedule={
        "heartbeat": {"task": "demo.heartbeat", "schedule": TICK_SECONDS},
    },
)

config = CeleryManagerConfig(
    celery_app=app,
    db_url=DB_URL,
    # Private and random in a real deployment; this default is for local use only.
    secret_key=os.environ.get("CM_EXAMPLE_SECRET", "local-example-secret-change-me-0123456789"),
    cookie_secure=False,       # plain HTTP on localhost; keep True behind HTTPS
    schedule_editing=True,     # beat runs DatabaseScheduler, so edits apply
    # USD per million tokens; used to price record_llm_usage() calls below.
    llm_prices={"example-model": {"input": 3.0, "output": 15.0}},
)
# Runs in every process: wires @track, progress and LLM recording.
celery_manager.setup(config)


@app.task(bind=True, name="demo.add")
@celery_manager.track()
def add(self, x, y):
    """A task that succeeds."""
    return x + y


@app.task(bind=True, name="demo.explode")
@celery_manager.track()
def explode(self):
    """A task that always fails: shows failure groups and one-click retry."""
    raise RuntimeError("demo failure: upstream service unavailable")


@app.task(bind=True, name="demo.import_rows")
@celery_manager.track()
def import_rows(self, rows=3):
    """Publishes live progress while it runs."""
    for i in range(1, rows + 1):
        celery_manager.report_progress(i, rows, f"row {i}")
    return rows


@app.task(bind=True, name="demo.summarize")
@celery_manager.track()
def summarize(self):
    """Pretends to call a language model and records tokens and cost.

    Only counts are stored, never prompts or completions.
    """
    celery_manager.record_llm_usage("example-model", 1200, 300)
    return "summary"


@app.task(bind=True, name="demo.heartbeat")
@celery_manager.track(schedule_name="heartbeat")
def heartbeat(self):
    """Fired by beat; appears under Schedules with pause/resume controls."""
    return "ok"


def make_dashboard():
    """ASGI app factory for uvicorn: the API plus the dashboard SPA."""
    from celery_manager.api.app import create_app

    asgi, _ = create_app(config)
    return asgi
