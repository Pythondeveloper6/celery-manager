"""Configuration shared by every process in this demo (web, worker, beat,
events listener). Read from environment variables so nothing secret is
committed."""
import os

from celery_manager.config import CeleryManagerConfig

# Set CELERY_MANAGER_EAGER=1 to run tasks inline with no Redis/worker (and an
# in-memory broker, so nothing needs to be installed or running).
EAGER = os.environ.get("CELERY_MANAGER_EAGER") == "1"
BROKER_URL = os.environ.get("CELERY_BROKER_URL", "memory://" if EAGER else "redis://localhost:6379/0")
RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "cache+memory://" if EAGER else BROKER_URL)
# celery-manager's own storage (task history, users, audit log). Any
# SQLAlchemy URL works; every process must point at the same database.
DB_URL = os.environ.get("CELERY_MANAGER_DB_URL", "sqlite:///celery_manager_demo.db")
# Plain-HTTP local development needs this False, or the browser will not
# send the Secure session cookie back. Leave it True behind HTTPS.
COOKIE_SECURE = os.environ.get("CELERY_MANAGER_COOKIE_SECURE", "0") == "1"


def secret_key() -> str:
    """The dashboard signs session cookies with this. Only the process that
    serves the dashboard (and create_user.py) needs it; create_app() refuses
    to start without a real one."""
    key = os.environ.get("CELERY_MANAGER_SECRET_KEY", "")
    if not key:
        raise SystemExit(
            "Set CELERY_MANAGER_SECRET_KEY to a private random value, e.g.\n"
            '  export CELERY_MANAGER_SECRET_KEY="$(python -c \'import secrets; print(secrets.token_urlsafe(32))\')"'
        )
    return key


def make_config(celery_app, *, with_secret: bool = False) -> CeleryManagerConfig:
    return CeleryManagerConfig(
        celery_app=celery_app,
        db_url=DB_URL,
        secret_key=secret_key() if with_secret else "unused-in-this-process",
        cookie_secure=COOKIE_SECURE,
    )
