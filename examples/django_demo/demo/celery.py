import os

# Workaround for an upstream Celery 5.6 / billiard 4.3 bug on macOS: billiard
# spawns (not forks) pool children there, and Celery only initialises a spawned
# child when this is set; without it every task fails with
# "ValueError: not enough values to unpack (expected 3, got 0)".
os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")

app = Celery("demo")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Beat's DatabaseScheduler (`celery -A demo beat -S
# celery_manager.beat:DatabaseScheduler`) reads schedule edits made in the
# dashboard from celery-manager's database, and finds it here.
from django.conf import settings

app.conf.celery_manager_db_url = settings.CELERY_MANAGER_DB_URL
