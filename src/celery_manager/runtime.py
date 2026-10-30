from __future__ import annotations

import os
import threading
import weakref

from sqlalchemy.pool import StaticPool

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.session import get_session_factory, init_db
from celery_manager.decorator import configure as _configure_decorator

_lock = threading.Lock()
_current: tuple[CeleryManagerConfig, object, object] | None = None
_engines = weakref.WeakSet()


def _drop_inherited_connections() -> None:
    """In a forked child (a prefork Celery worker process), forget the pooled
    connections inherited from the parent. Parent and child would otherwise
    share one database socket, which on Postgres shows up as
    `ResourceClosedError`, wrong rows and slow tasks. `close=False` leaves the
    parent's connections alone; the child opens its own on first use. A
    StaticPool (in-memory SQLite) is the database itself, so it is kept."""
    for engine in list(_engines):
        if not isinstance(engine.pool, StaticPool):
            engine.dispose(close=False)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_drop_inherited_connections)


def session_factory_for(celery_app):
    """The configured factory for this exact app, or None if not configured."""
    state = getattr(celery_app, "_cm_runtime_state", None)
    return state.sessions if state is not None else None


def setup(config: CeleryManagerConfig):
    """Wire celery-manager into the current process. Returns the session factory.

    Call this **once per process that touches celery-manager**, which
    critically includes the **Celery worker process** — not just the process
    that builds the API app. `@track`-decorated tasks execute in the worker,
    and the decorator's session factory is process-local module state: if the
    worker never calls this, `@track` degrades to a pass-through and the
    pause/skip feature and log capture silently never engage. That was the
    normal case before this function existed, since `create_app()` (API
    process only) was the sole caller of `decorator.configure()`.

    Typical worker wiring — at import time of the module that defines your
    tasks, so it runs before any task does::

        import celery_manager
        from celery_manager.config import CeleryManagerConfig

        celery_manager.setup(CeleryManagerConfig(
            celery_app=app,
            db_url="sqlite:///celery_manager.db",
            secret_key="...",
        ))

    Idempotent: calling it repeatedly with the *same* config object reuses the
    engine and session factory already built for it rather than opening a
    second engine and re-running migrations. Each Celery app keeps its own
    runtime: configuring another app never changes storage or policies for
    tasks already bound to the first app. Configuring the same app with a
    different config replaces that app's runtime for subsequent invocations.
    The per-task ContextVar keeps nested and concurrent executions isolated.
    """
    global _current
    with _lock:
        record = getattr(config, "_cm_runtime_record", None)
        if record is not None:
            session_factory = record[2]
            _current = record
        else:
            engine = init_db(config)
            session_factory = get_session_factory(engine)
            session_factory.configure(info={"cm_config": config})
            _current = (config, engine, session_factory)
            config._cm_runtime_record = _current
            _engines.add(engine)
        # A monitoring-only runtime must not change an existing Celery app's
        # decorator globals or claim to be a Celery executor.
        if config.celery_app is not None:
            _configure_decorator(session_factory, eta_tolerance_seconds=config.eta_tolerance_seconds, config=config)
        return session_factory
