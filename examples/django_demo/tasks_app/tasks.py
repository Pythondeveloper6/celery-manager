import logging
import random
import time

from celery import shared_task
from celery.schedules import crontab
from demo.celery import app as celery_app
from demo.cm import config as cm_config

import celery_manager
from celery_manager import report_progress
from celery_manager.tasks import register_prune_task

# This module is imported by the Celery WORKER process, which is where the
# @celery_manager.track() tasks below actually execute — so this is the
# process that has to be wired. create_app() (run_dashboard.py) wires the
# dashboard process only; without the call below, the worker's decorators
# would silently no-op and neither per-occurrence pause/skip nor log capture
# would ever engage. One call, at import time, before any task can run.
#
# celery_manager.setup() is idempotent and does the whole job (engine +
# create_all + handing the decorator a session factory).
#
# The config itself lives in demo/cm.py so the dashboard, this worker and the
# events process all share one.
session_factory = celery_manager.setup(cm_config)


# Opt-in retention: a daily beat entry ("celery-manager-prune", 03:00) that
# deletes runs older than the retention rules (see `demo_setup`, which sets
# 30 days, failed runs 90).
register_prune_task(celery_app, cm_config, schedule=crontab(hour=3, minute=0))

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


@shared_task(bind=True)
@celery_manager.track(schedule_name="heartbeat-every-minute")
def heartbeat(self):
    """Beat runs this every minute and it never fails: the easy way to see
    missed runs (stop beat), maintenance windows and schedule edits."""
    logger.info("heartbeat")
    time.sleep(0.2)
    return "ok"


@shared_task(
    bind=True,
    autoretry_for=(ConnectionError,),
    max_retries=2,
    retry_kwargs={"countdown": 3},
)
@celery_manager.track(schedule_name="sync-inventory-every-5-minutes")
def sync_inventory(self):
    """Fails about 40% of the time with a ConnectionError. Celery retries it
    twice; a run that fails all three times ends as a dead letter, and the
    retry chain shows on the run's detail page."""
    logger.info("syncing inventory (attempt %s)", self.request.retries + 1)
    time.sleep(1)
    if random.random() < 0.4:
        raise ConnectionError("inventory API refused the connection (demo failure)")
    return "inventory synced"


@shared_task(bind=True)
@celery_manager.track()
def charge_card(self, customer_id: int, amount: int):
    """Declines anything over 500. Find the failed run under Dead letters,
    choose "edit arguments and run again", give it a smaller amount and it
    succeeds."""
    logger.info("charging customer %s %s", customer_id, amount)
    time.sleep(0.5)
    if amount > 500:
        raise ValueError(f"card declined: {amount} is over the 500 limit for customer {customer_id}")
    return f"charged customer {customer_id} {amount}"


@shared_task(bind=True)
@celery_manager.track()
def rebuild_search_index(self, pages: int = 40):
    """Slow, with a known total, and routed to the "reports" queue (see
    CELERY_TASK_ROUTES in settings.py). A burst of these backs the queue up,
    which is what the queue-backlog alert and the Queues page show."""
    logger.info("rebuilding the search index, %s pages", pages)
    for page in range(1, pages + 1):
        time.sleep(1)
        report_progress(page, pages, f"page {page} of {pages}")
    return f"indexed {pages} pages"
