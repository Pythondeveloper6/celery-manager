from __future__ import annotations

from dataclasses import dataclass

from celery import Celery


@dataclass
class CeleryManagerConfig:
    """Runtime configuration for celery-manager.

    celery_app: the host application's Celery instance. Used for task
        introspection (registered task names) and control calls
        (revoke/terminate).
    db_url: SQLAlchemy connection URL for celery-manager's own storage.
        Defaults to a local SQLite file; swap to any SQLAlchemy-supported
        DB (Postgres, MySQL, ...) for production use.
    secret_key: used to sign session cookies for the built-in auth.
    eta_tolerance_seconds: how close "now" must be to a schedule
        override's target time for the decorator to treat it as a match.
    cookie_secure: sets the `Secure` flag on the session cookie, so the
        browser only ever sends it over HTTPS. Defaults to True — the
        session cookie is a bearer credential for the whole dashboard, and
        a default of False would silently ship every deployment that never
        thought about it with a cookie leakable over plain HTTP. Set it to
        False only for local HTTP development, where there is no HTTPS to
        send it over.
    """

    celery_app: Celery
    db_url: str = "sqlite:///celery_manager.db"
    secret_key: str = "change-me"
    eta_tolerance_seconds: int = 60
    cookie_secure: bool = True
