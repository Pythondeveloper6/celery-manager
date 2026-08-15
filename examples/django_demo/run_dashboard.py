import os

import django
import uvicorn

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")
django.setup()

from django.conf import settings

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from demo.celery import app as celery_app

config = CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
)
app, _ = create_app(config)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
