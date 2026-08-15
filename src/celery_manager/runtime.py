from __future__ import annotations

import threading

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.session import get_session_factory, init_db
from celery_manager.decorator import configure as _configure_decorator

_lock = threading.Lock()
_current: tuple[CeleryManagerConfig, object, object] | None = None


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
    second engine and re-running `create_all`. Calling it with a *different*
    config builds a fresh engine and re-points the decorator at it — which is
    what makes it safe for tests (each builds its own config, often against
    its own `sqlite:///:memory:` database) and for the rare process that
    genuinely manages more than one configuration.
    """
    global _current
    with _lock:
        if _current is not None and _current[0] is config:
            session_factory = _current[2]
        else:
            engine = init_db(config)
            session_factory = get_session_factory(engine)
            _current = (config, engine, session_factory)
        _configure_decorator(session_factory, eta_tolerance_seconds=config.eta_tolerance_seconds)
        return session_factory
