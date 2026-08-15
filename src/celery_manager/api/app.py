from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.session import init_db, get_session_factory
from celery_manager.decorator import configure as configure_decorator
from celery_manager.api.routes import auth as auth_routes, tasks as task_routes, schedules as schedule_routes
from celery_manager.control.schedules import sync_periodic_schedules
from celery_manager.api.sse import EventBus, sse_stream


def create_app(config: CeleryManagerConfig):
    """Build the celery-manager FastAPI app. Returns (app, session_factory)
    — callers that need direct DB access (tests, the events listener CLI)
    use the session_factory; HTTP consumers just use the app."""
    engine = init_db(config)
    session_factory = get_session_factory(engine)
    configure_decorator(session_factory, eta_tolerance_seconds=config.eta_tolerance_seconds)

    with session_factory() as session:
        sync_periodic_schedules(session, config.celery_app)

    app = FastAPI(title="celery-manager")
    app.state.cm_config = config
    app.state.cm_session_factory = session_factory
    app.state.cm_event_bus = EventBus()

    app.include_router(auth_routes.router)
    app.include_router(task_routes.router)
    app.include_router(schedule_routes.router)

    @app.get("/api/stream")
    def stream(request: Request):
        return StreamingResponse(sse_stream(app.state.cm_event_bus), media_type="text/event-stream")

    return app, session_factory
