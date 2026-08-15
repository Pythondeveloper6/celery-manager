import logging
import random
import time

from celery import shared_task

from celery_manager.decorator import track
from demo.celery import app as celery_app
from celery_manager.config import CeleryManagerConfig
from celery_manager.decorator import configure
from celery_manager.db.session import init_db, get_session_factory
from django.conf import settings

_config = CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
)
_engine = init_db(_config)
configure(get_session_factory(_engine), eta_tolerance_seconds=_config.eta_tolerance_seconds)

logger = logging.getLogger(__name__)


@shared_task(bind=True)
@track()
def send_welcome_email(self, user_email: str):
    logger.info("sending welcome email to %s", user_email)
    time.sleep(1)
    return f"sent to {user_email}"


@shared_task(bind=True)
@track(schedule_name="send-demo-report-every-2-hours")
def send_report(self):
    logger.info("generating demo report")
    if random.random() < 0.3:
        raise RuntimeError("report generator flaked (demo failure)")
    time.sleep(2)
    return "report sent"
