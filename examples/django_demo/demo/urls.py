from django.conf import settings
from django.urls import path
from django.http import HttpResponse

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from demo.celery import app as celery_app

_cm_config = CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
)
cm_app, _ = create_app(_cm_config)

urlpatterns = [
    path("", lambda request: HttpResponse("Django demo app. Dashboard mounted at /dashboard/ (run via ASGI).")),
]
