from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from celery_manager.api.deps import get_current_user
from celery_manager.api.routes import (
    analytics as analytics_routes,
)
from celery_manager.api.routes import (
    audit as audit_routes,
)
from celery_manager.api.routes import (
    auth as auth_routes,
)
from celery_manager.api.routes import (
    export as export_routes,
)
from celery_manager.api.routes import (
    schedules as schedule_routes,
)
from celery_manager.api.routes import (
    settings as settings_routes,
)
from celery_manager.api.routes import (
    tasks as task_routes,
)
from celery_manager.api.routes import (
    workers as worker_routes,
)
from celery_manager.api.security import SecurityMiddleware
from celery_manager.api.sse import EventBus, sse_stream
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.schedules import sync_periodic_schedules
from celery_manager.runtime import setup as setup_runtime

# The placeholder `CeleryManagerConfig.secret_key` ships with. It is public —
# it's in this file and the docs — so a session cookie signed with it can be
# forged for any username by anyone.
_DEFAULT_SECRET_KEY = "change-me"


def create_app(config: CeleryManagerConfig):
    """Build the celery-manager FastAPI app. Returns (app, session_factory)
    — callers that need direct DB access (tests, the events listener CLI)
    use the session_factory; HTTP consumers just use the app."""
    # Checked here rather than in CeleryManagerConfig: worker and listener
    # processes build the same config but never sign or verify a session, so
    # they're free to leave secret_key alone. Only a process serving HTTP is
    # a forgery risk, and it should refuse to start, not start insecurely.
    if not config.secret_key or not config.secret_key.strip() or config.secret_key == _DEFAULT_SECRET_KEY:
        raise ValueError(
            "CeleryManagerConfig.secret_key is unset or still the default "
            f"{_DEFAULT_SECRET_KEY!r}; anyone could forge a dashboard session with it. "
            "Set it to a private random value, e.g. the output of "
            '`python -c "import secrets; print(secrets.token_urlsafe(32))"`.'
        )

    # Single shared entry point for "wire celery-manager into this process"
    # (init_db + session factory + decorator configure) rather than an inline
    # copy of it, so the API process and the worker process — which must call
    # `celery_manager.setup(config)` itself, see runtime.setup's docstring —
    # can never drift out of sync.
    session_factory = setup_runtime(config)

    with session_factory() as session:
        sync_periodic_schedules(session, config.celery_app)

    app = FastAPI(title="celery-manager")
    app.state.cm_config = config
    app.state.cm_session_factory = session_factory
    app.state.cm_event_bus = EventBus()
    app.add_middleware(SecurityMiddleware)

    app.include_router(auth_routes.router)
    # Before task_routes: /api/tasks/export must win over /api/tasks/{task_id}.
    app.include_router(export_routes.router)
    app.include_router(task_routes.router)
    app.include_router(schedule_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(analytics_routes.router)
    app.include_router(worker_routes.router)

    @app.get("/api/stream")
    def stream(request: Request, username: str = Depends(get_current_user)):
        return StreamingResponse(sse_stream(app.state.cm_event_bus), media_type="text/event-stream")

    static_dir = Path(__file__).parent.parent / "dashboard" / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")

    return app, session_factory
