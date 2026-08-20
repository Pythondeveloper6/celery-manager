import logging
import random
import time

from celery import shared_task
from django.conf import settings

import celery_manager
from celery_manager import report_progress
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
@celery_manager.track()
def import_customers(self, row_count: int = 60):
    """A deliberately slow task, so the dashboard's live progress bar has
    something real to show.

    `report_progress(current, total, message)` is the whole API: call it from
    inside the task body as often as is natural, and celery-manager throttles
    the actual database writes (see `celery_manager.progress`) so a tight loop
    doesn't turn into one commit per iteration.

    Run it and watch the task list:

        python manage.py shell -c \\
            "from tasks_app.tasks import import_customers; import_customers.delay(60)"
    """
    logger.info("importing %s customer rows", row_count)
    for row in range(1, row_count + 1):
        time.sleep(0.5)
        report_progress(row, row_count, f"row {row} of {row_count}")
    return f"imported {row_count} rows"


@shared_task(bind=True)
@celery_manager.track()
def tail_event_stream(self, seconds: int = 30):
    """Progress with no known total — the other supported shape.

    Some work genuinely has no denominator: draining a queue, following a
    stream, scanning until exhausted. Passing `total=None` (just omitting it)
    reports a running count and a message with no percentage, and the
    dashboard shows exactly that rather than inventing a progress bar it
    cannot honestly fill.
    """
    logger.info("tailing the demo event stream for %ss", seconds)
    seen = 0
    for _ in range(seconds):
        time.sleep(1)
        seen += random.randint(1, 9)
        report_progress(seen, message=f"{seen} events seen")
    return f"observed {seen} events"


@shared_task(bind=True)
@celery_manager.track(schedule_name="send-demo-report-every-2-hours")
def send_report(self):
    logger.info("generating demo report")
    if random.random() < 0.3:
        raise RuntimeError("report generator flaked (demo failure)")
    time.sleep(2)
    return "report sent"
