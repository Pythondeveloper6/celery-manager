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
    secret_key: used to sign session cookies for the built-in auth. The
        default is a public placeholder: `create_app()` refuses to start
        with it (or an empty key), since anyone could forge a session from
        it. Processes that don't serve HTTP (workers, the events listener)
        never sign sessions and may leave it as-is.
    eta_tolerance_seconds: how close "now" must be to a schedule
        override's target time for the decorator to treat it as a match.
    cookie_secure: sets the `Secure` flag on the session cookie, so the
        browser only ever sends it over HTTPS. Defaults to True — the
        session cookie is a bearer credential for the whole dashboard, and
        a default of False would silently ship every deployment that never
        thought about it with a cookie leakable over plain HTTP. Set it to
        False only for local HTTP development, where there is no HTTPS to
        send it over.
    inspect_timeout_seconds: how long the Workers page (and the worker
        control endpoints) wait for workers to answer a broadcast
        (`inspect()`, `pool_grow`, `rate_limit`, ...). Kept short because
        the request blocks for it: a worker that hasn't answered within a
        second is, for an operator, effectively offline.
    """

    celery_app: Celery
    db_url: str = "sqlite:///celery_manager.db"
    secret_key: str = "change-me"
    eta_tolerance_seconds: int = 60
    cookie_secure: bool = True
    inspect_timeout_seconds: float = 1.0
