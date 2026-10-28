from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import path

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from demo.celery import app as celery_app

_cm_config = CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
    cookie_secure=settings.CELERY_MANAGER_COOKIE_SECURE,
)
cm_app, _ = create_app(_cm_config)

urlpatterns = [
    # The Django admin: sign in here and the dashboard on :8001 signs you in
    # too (run_dashboard.py's django_authenticator).
    path("admin/", admin.site.urls),
    # The dashboard is deliberately NOT mounted here — `cm_app` above is built
    # only to prove it constructs cleanly against Django's settings. WSGI
    # cannot serve an ASGI app in-process, so the dashboard runs as its own
    # uvicorn process (`run_dashboard.py`, port 8001). See the demo README's
    # "Why a separate dashboard process".
    path("", lambda request: HttpResponse(
        "Django demo app. The celery-manager dashboard runs as a separate "
        "process: python run_dashboard.py -> http://localhost:8001"
    )),
]
