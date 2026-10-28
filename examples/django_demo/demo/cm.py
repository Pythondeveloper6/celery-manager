"""The one celery-manager config every process shares.

`run_dashboard.py` (the dashboard), `tasks_app/tasks.py` (the worker's
`celery_manager.setup`) and the events/alerts process
(`celery-manager events --config demo.cm:config --with-alerts`) all import
`config` from here, so they cannot drift apart on the database, the secret
key, the SMTP settings or the dashboard address.

This module is imported while Django is still loading its apps (tasks_app
imports it), so it must never call `django.setup()`. Reading values from
`django.conf.settings` is fine: settings load lazily on first access.
"""

import os

from django.conf import settings

from celery_manager.config import CeleryManagerConfig
from demo.celery import app as celery_app

config = CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
    # False because this demo is served over plain HTTP — see settings.py.
    cookie_secure=settings.CELERY_MANAGER_COOKIE_SECURE,
    # Lets operators change a schedule's timing from the dashboard; beat picks
    # the edit up through DatabaseScheduler (see demo/celery.py).
    schedule_editing=True,
    # GET /metrics needs `Authorization: Bearer <this>`. Unset means no endpoint.
    metrics_token=os.environ.get("CELERY_MANAGER_METRICS_TOKEN") or None,
    # Links in alert messages point here.
    dashboard_url=os.environ.get("CELERY_MANAGER_DASHBOARD_URL", "http://localhost:8001/"),
    # Email alerts go to Mailpit, a catch-all inbox (UI on :8025).
    smtp_host=os.environ.get("SMTP_HOST", "mailpit"),
    smtp_port=int(os.environ.get("SMTP_PORT", "1025")),
    smtp_use_tls=False,
    alert_email_from="celery-manager@example.com",
    # Default is 300s; a minute makes "stop beat" show missed runs sooner.
    missed_run_grace_seconds=60,
)
