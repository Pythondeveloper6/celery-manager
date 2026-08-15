import logging
import random
import time

from celery import shared_task
from django.conf import settings

import celery_manager
from celery_manager.config import CeleryManagerConfig
from demo.celery import app as celery_app

# This module is imported by the Celery WORKER process, which is where the
# @celery_manager.track() tasks below actually execute — so this is the
# process that has to be wired. create_app() (run_dashboard.py) wires the
# dashboard process only; without the call below, the worker's decorators
# would silently no-op and neither per-occurrence pause/skip nor log capture
# would ever engage. One call, at import time, before any task can run.
#
# celery_manager.setup() is idempotent and does the whole job (engine +
# create_all + handing the decorator a session factory).
celery_manager.setup(CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
))

logger = logging.getLogger(__name__)


@shared_task(bind=True)
@celery_manager.track()
def send_welcome_email(self, user_email: str):
    logger.info("sending welcome email to %s", user_email)
    time.sleep(1)
    return f"sent to {user_email}"


@shared_task(bind=True)
@celery_manager.track(schedule_name="send-demo-report-every-2-hours")
def send_report(self):
    logger.info("generating demo report")
    if random.random() < 0.3:
        raise RuntimeError("report generator flaked (demo failure)")
    time.sleep(2)
    return "report sent"
