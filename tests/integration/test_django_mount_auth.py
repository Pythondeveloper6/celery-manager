"""Real Django ASGI host + prefixed dashboard + shared Django sessions.

Django settings are process-global; subprocess isolation lets this contract run
alongside both the Django auth tests and the optional django-celery-beat tests.
No web server or socket is needed: both real ASGI apps run through TestClient.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = r'''
import re
import sys
from django.conf import settings
settings.configure(
    SECRET_KEY="django-mount-contract-secret",
    ROOT_URLCONF=__name__,
    ALLOWED_HOSTS=["testserver"],
    INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions"],
    MIDDLEWARE=["django.contrib.sessions.middleware.SessionMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware"],
    DATABASES={"default":{"ENGINE":"django.db.backends.sqlite3","NAME":sys.argv[1]}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    USE_TZ=True,
)
import django
django.setup()
from django.core.management import call_command
call_command("migrate", verbosity=0, interactive=False)
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.asgi import get_asgi_application
from django.http import JsonResponse
from django.test import Client
from django.urls import path

def host_view(request):
    return JsonResponse({"framework":"django", "user":request.user.get_username()})
urlpatterns=[path("site/",host_view)]

from celery import Celery
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount
from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.integrations.django import django_authenticator

config=CeleryManagerConfig(celery_app=Celery("django-mount-contract"),
    db_url="sqlite:///:memory:", secret_key="dashboard-mount-secret",
    authenticate_request=django_authenticator(), local_login_enabled=False,
    cookie_secure=False, live_stream="poll")
dashboard,_=create_app(config)
host=Starlette(routes=[Mount("/celery-manager",dashboard),Mount("/",get_asgi_application())])
client=TestClient(host,base_url="http://testserver")
root=client.get("/celery-manager/")
assert root.status_code==200 and '<div id="root">' in root.text
assets=re.findall(r'(?:src|href)="([^"]+)"',root.text)
script=next(p for p in assets if p.endswith(".js"))
assert script.startswith("./")
assert client.get("/celery-manager/"+script[2:]).status_code==200
assert client.get("/celery-manager/api/whoami").status_code==401
assert client.post("/celery-manager/api/auth/login",json={"username":"root","password":"pw"}).status_code==403

user=get_user_model().objects.create_user(username="django-root",password="pw",is_superuser=True,is_staff=True)
django_client=Client()
django_client.force_login(user)
key=django_client.cookies[settings.SESSION_COOKIE_NAME].value
client.cookies.set(settings.SESSION_COOKIE_NAME,key)
before=Session.objects.get(session_key=key).session_data
assert client.get("/site/").json()=={"framework":"django","user":"django-root"}
identity=client.get("/celery-manager/api/whoami")
assert identity.status_code==200 and identity.json()["role"]=="admin"
assert identity.json()["source"]=="external"
assert client.put("/celery-manager/api/settings",json={"brand_name":"Django host"},headers={"Origin":"http://testserver"}).status_code==200
assert client.put("/celery-manager/api/settings",json={"brand_name":"Blocked"},headers={"Origin":"https://other.example"}).status_code==403
assert Session.objects.get(session_key=key).session_data==before

user.is_superuser=False
user.save()
assert client.get("/celery-manager/api/whoami").json()["role"]=="operator"
assert client.put("/celery-manager/api/settings",json={"brand_name":"Not admin"},headers={"Origin":"http://testserver"}).status_code==403
user.is_staff=False
user.save()
assert client.get("/celery-manager/api/whoami").status_code==403
user.is_staff=True
user.is_active=False
user.save()
assert client.get("/celery-manager/api/whoami").status_code==401
user.is_active=True
user.set_password("changed-password")
user.save()
assert client.get("/celery-manager/api/whoami").status_code==401
assert Session.objects.get(session_key=key).session_data==before
print("Django ASGI mounting, assets, real sessions, roles, CSRF and revocation passed")
'''


def test_real_django_asgi_mount_and_shared_authentication(tmp_path):
    pytest.importorskip("django")
    source = Path(__file__).resolve().parents[2] / "src"
    env = {**os.environ, "PYTHONPATH": str(source)}
    result = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path / "django.sqlite3")],
                            env=env, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
