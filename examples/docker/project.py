"""The whole example deployment in one module: Celery app, tasks, the
celery-manager config shared by every process, and the dashboard ASGI app.

Every process imports this module:
  web     uvicorn project:dashboard
  worker  celery -A project worker
  beat    celery -A project beat -S celery_manager.beat:Scheduler
  events  celery-manager events --config project:config
"""
import os
import random
import time
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab

import celery_manager
from celery_manager import CeleryManagerConfig

app = Celery("project", broker=os.environ["BROKER_URL"], backend=os.environ.get("RESULT_BACKEND"))
app.conf.update(
    timezone="UTC",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_send_task_events=True,
    task_send_sent_event=True,
    task_track_started=True,
    task_soft_time_limit=300,
    task_time_limit=360,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 7200},
    # Monitoring database for beat's liveness heartbeat.
    celery_manager_db_url=os.environ["CM_DB_URL"],
    beat_schedule={
        "refresh-cache": {"task": "project.refresh_cache", "schedule": timedelta(minutes=1)},
        "nightly-report": {"task": "project.build_report", "schedule": crontab(minute="0", hour="2")},
    },
)

config = CeleryManagerConfig(
    celery_app=app,
    db_url=os.environ["CM_DB_URL"],
    secret_key=os.environ["CM_SECRET_KEY"],
    # Plain HTTP on localhost in this example; keep the default (True)
    # behind HTTPS in a real deployment.
    cookie_secure=os.environ.get("CM_COOKIE_SECURE", "true").lower() == "true",
    environment_name=os.environ.get("CM_ENVIRONMENT", "docker-example"),
    auto_migrate=os.environ.get("CM_AUTO_MIGRATE", "false").lower() == "true",
)
celery_manager.setup(config)


@app.task(bind=True, name="project.refresh_cache")
@celery_manager.track(schedule_name="refresh-cache")
def refresh_cache(self):
    time.sleep(random.uniform(0.1, 1.5))
    return {"keys": random.randint(10, 100)}


@app.task(bind=True, name="project.build_report")
@celery_manager.track(schedule_name="nightly-report")
def build_report(self, rows=500):
    for i in range(rows):
        if i % 50 == 0:
            celery_manager.report_progress(i, rows, "building report")
    if random.random() < 0.2:
        raise RuntimeError("upstream report service unavailable")
    return {"rows": rows}


def dashboard_app():
    from celery_manager.api.app import create_app

    asgi, _ = create_app(config)
    return asgi


dashboard = dashboard_app() if os.environ.get("CM_ROLE") == "web" else None
