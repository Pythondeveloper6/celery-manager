import os

import django
import uvicorn

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")
django.setup()

from dataclasses import replace

from demo.cm import config as cm_config

from celery_manager.api.app import create_app
from celery_manager.integrations.django import django_authenticator

# Shared with the worker and the events process: see demo/cm.py.
config = replace(
    cm_config,
    # Sign in with the Django site's users: anyone signed in to the Django
    # admin (http://localhost:8000/admin/) is signed in here too, superusers
    # as admin and other staff as operator. The browser sends Django's
    # `sessionid` cookie to port 8001 as well, since cookies are per host,
    # not per port. Local dashboard accounts keep working alongside.
    authenticate_request=django_authenticator(),
    # No {next}: Django only redirects back to its own host and port, and the
    # dashboard is on another port. Sign in, then come back to :8001.
    login_url="http://localhost:8000/admin/login/",
    login_provider_name="the Django admin",
)
app, _ = create_app(config)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
