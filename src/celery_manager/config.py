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
    eta_tolerance_seconds: how far apart two timestamps of the same
        interval-schedule occurrence may be and still match. With beat
        running the stamping scheduler (`celery_manager.beat:Scheduler`) the
        decorator matches a skip/cancel by the occurrence beat stamped on the
        message: crontab/solar exactly, interval schedules within this
        window (capped at half the interval). The dashboard also uses it to
        match saved overrides to listed occurrences. Only for messages with
        no occurrence headers from a beat that has never stamped one (an
        older beat during an upgrade) is it still compared with "now", the
        previous behaviour.
    missed_run_grace_seconds: how long after a scheduled run was due it may
        still arrive before the dashboard reports it as missed. Detection
        needs beat to run the stamping scheduler
        (`celery_manager.beat:Scheduler`); see the decorator docs. Raise it
        if your broker or workers are routinely slow to start a run.
    export_max_rows: hard cap on rows in one `GET /api/tasks/export` download.
        Larger results are cut off (newest first) and flagged with an
        `X-Export-Truncated: true` header.
    cookie_secure: sets the `Secure` flag on the session cookie, so the
        browser only ever sends it over HTTPS. Defaults to True — the
        session cookie is a bearer credential for the whole dashboard, and
        a default of False would silently ship every deployment that never
        thought about it with a cookie leakable over plain HTTP. Set it to
        False only for local HTTP development, where there is no HTTPS to
        send it over.
    auto_migrate: when True (the default), `init_db()` upgrades the
        database schema to the latest revision at startup, in every process
        that calls it (API, workers, events listener; concurrent upgrades
        are serialised by a lock). Set it to False to run migrations as a
        separate deploy step with `celery-manager migrate`; `init_db()`
        then leaves the database untouched and only logs a warning if the
        schema isn't current.
    inspect_timeout_seconds: how long the Workers page (and the worker
        control endpoints) wait for workers to answer a broadcast
        (`inspect()`, `pool_grow`, `rate_limit`, ...). Kept short because
        the request blocks for it: a worker that hasn't answered within a
        second is, for an operator, effectively offline.
    live_poll_interval_seconds: how often, while at least one dashboard
        has the live stream (`/api/stream`) open, each API process polls
        `task_runs` for rows other processes changed (the events listener,
        workers) and pushes them to the browser. Lower is snappier and
        costs one small indexed query per interval per API process; nothing
        is polled while nobody is watching.
    """

    celery_app: Celery
    db_url: str = "sqlite:///celery_manager.db"
    secret_key: str = "change-me"
    eta_tolerance_seconds: int = 60
    missed_run_grace_seconds: int = 300
    cookie_secure: bool = True
    export_max_rows: int = 50_000
    auto_migrate: bool = True
    inspect_timeout_seconds: float = 1.0
    live_poll_interval_seconds: float = 1.0
