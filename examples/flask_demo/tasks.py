import logging
import random
import time

import settings
from celery_app import app

import celery_manager
from celery_manager import report_progress

# This module is imported by the Celery WORKER, which is where @track tasks
# actually run, so the worker process has to be wired too. Without this call
# the decorators silently no-op (no pause/skip, no log capture, no progress).
# The worker never serves HTTP, so it needs no secret key.
celery_manager.setup(settings.make_config(app))

logger = logging.getLogger(__name__)


@app.task(bind=True)
@celery_manager.track()
def send_welcome_email(self, user_email: str):
    logger.info("sending welcome email to %s", user_email)
    time.sleep(1)
    return f"sent to {user_email}"


@app.task(bind=True)
@celery_manager.track()
def import_customers(self, row_count: int = 60):
    """Slow on purpose: gives the dashboard a live progress bar to show."""
    logger.info("importing %s customer rows", row_count)
    for row in range(1, row_count + 1):
        time.sleep(0.5)
        report_progress(row, row_count, f"row {row} of {row_count}")
    return f"imported {row_count} rows"


@app.task(bind=True)
@celery_manager.track(schedule_name="send-demo-report-every-2-hours")
def send_report(self):
    logger.info("generating demo report")
    if random.random() < 0.3:
        raise RuntimeError("report generator flaked (demo failure)")
    time.sleep(2)
    return "report sent"
