from __future__ import annotations

from fastapi import FastAPI

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.session import init_db, get_session_factory
from celery_manager.decorator import configure as configure_decorator
from celery_manager.api.routes import auth as auth_routes, tasks as task_routes, schedules as schedule_routes
from celery_manager.control.schedules import sync_periodic_schedules


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

    app.include_router(auth_routes.router)
    app.include_router(task_routes.router)
    app.include_router(schedule_routes.router)

    return app, session_factory
