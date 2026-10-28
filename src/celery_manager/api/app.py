from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from celery_manager.api.deps import REQUIRED_ROLE_ATTR, get_current_user
from celery_manager.api.routes import (
    alerts as alert_routes,
)
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
    deadletters as deadletter_routes,
)
from celery_manager.api.routes import (
    export as export_routes,
)
from celery_manager.api.routes import (
    metrics as metrics_routes,
    maintenance as maintenance_routes,
)
from celery_manager.api.routes import (
    queues as queue_routes,
)
from celery_manager.api.routes import (
    retention as retention_routes,
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
    users as user_routes,
)
from celery_manager.api.routes import (
    workers as worker_routes,
)
from celery_manager.api.routes import (
    workflows as workflow_routes,
)
from celery_manager.api.security import SecurityMiddleware
from celery_manager.api.sse import EventBus, sse_stream
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.schedules import sync_periodic_schedules
from celery_manager.metrics import MetricsCollector
from celery_manager.runtime import setup as setup_runtime

# The placeholder `CeleryManagerConfig.secret_key` ships with. It is public —
# it's in this file and the docs — so a session cookie signed with it can be
# forged for any username by anyone.
_DEFAULT_SECRET_KEY = "change-me"

# Methods a route may serve without declaring a role (any signed-in account
# may read). Everything else changes state and must use `require_role`.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The only state-changing routes that need no role: they run before (login)
# or regardless of (logout) a session.
PUBLIC_WRITE_ROUTES = frozenset({("POST", "/api/auth/login"), ("POST", "/api/auth/logout")})


def _role_of(call) -> str | None:
    return getattr(call, REQUIRED_ROLE_ATTR, None)


def route_required_role(route: APIRoute, extra_dependencies=()) -> str | None:
    """The minimum role a route declares via `require_role`, or None.
    `extra_dependencies` are `Depends(...)` added where its router was
    included."""
    for dep in extra_dependencies:
        role = _role_of(getattr(dep, "dependency", None))
        if role is not None:
            return role
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        role = _role_of(dependant.call)
        if role is not None:
            return role
        pending.extend(dependant.dependencies)
    return None


def iter_api_routes(routes, prefix: str = "", dependencies=()):
    """Yield (full path, APIRoute, include-level dependencies) for every API
    route, however deeply its router was included.

    Older FastAPI copies an included router's routes into the parent;
    newer releases (0.16x) keep the included router whole behind a wrapper
    exposing `original_router` and `include_context`. Both are walked.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route, tuple(dependencies)
        elif hasattr(route, "original_router"):
            context = getattr(route, "include_context", None)
            yield from iter_api_routes(
                route.original_router.routes,
                prefix + (getattr(context, "prefix", "") or ""),
                (*dependencies, *(getattr(context, "dependencies", None) or ())),
            )


def unclassified_write_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) that can change state but declares no role."""
    missing = []
    found = False
    for path, route, dependencies in iter_api_routes(app.routes):
        found = True
        for method in sorted(route.methods - _READ_METHODS):
            if (method, path) in PUBLIC_WRITE_ROUTES:
                continue
            if route_required_role(route, dependencies) is None:
                missing.append((method, path))
    if not found and app.routes:
        # A FastAPI release that hides routes some new way must not turn
        # this check into a silent pass.
        raise RuntimeError("could not enumerate the app's API routes to check their roles")
    return missing


def _check_link(name: str, url: str | None) -> None:
    """`login_url` / `logout_url` end up in an `<a href>`. Only a same-site
    path or an http(s) URL: a `javascript:` URL there would run in the
    dashboard's origin for everyone who clicks it."""
    if url is None:
        return
    # Browsers read `\` as `/` (so `/\evil.example` is another host) and
    # drop tabs and newlines inside URLs, so neither may appear at all.
    if isinstance(url, str) and url.isprintable() and "\\" not in url and " " not in url:
        parts = urlsplit(url)
        if (parts.scheme in ("http", "https") and parts.netloc) or (
            # A path, but not `//evil.example` (scheme-relative: another host).
            not parts.scheme and not parts.netloc and url.startswith("/") and not url.startswith("//")
        ):
            return
    raise ValueError(f"CeleryManagerConfig.{name} must be a path starting with '/' or an http(s) URL, not {url!r}")


def _check_sign_in_options(config: CeleryManagerConfig) -> None:
    if config.authenticate_request is not None and not callable(config.authenticate_request):
        raise ValueError("CeleryManagerConfig.authenticate_request must be a callable taking the request")
    if not config.local_login_enabled and config.authenticate_request is None:
        raise ValueError(
            "CeleryManagerConfig.local_login_enabled is False but no authenticate_request is set, "
            "so nobody could sign in. Set authenticate_request (see celery_manager.integrations) "
            "or leave local login on."
        )
    _check_link("login_url", config.login_url)
    _check_link("logout_url", config.logout_url)


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

    _check_sign_in_options(config)

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
    # The bus polls the DB change feed for writes made by other processes
    # (events listener, workers); see api/sse.py.
    app.state.cm_event_bus = EventBus(session_factory, poll_interval=config.live_poll_interval_seconds)
    # Shared by /metrics and the optional OpenTelemetry export, so both read
    # one cached snapshot. Built even when metrics are disabled: it does no
    # work until something asks.
    app.state.cm_metrics = MetricsCollector(config, session_factory)
    app.add_middleware(SecurityMiddleware)

    app.include_router(auth_routes.router)
    # Before task_routes: /api/tasks/export must win over /api/tasks/{task_id}.
    app.include_router(export_routes.router)
    app.include_router(task_routes.router)
    app.include_router(deadletter_routes.router)
    app.include_router(schedule_routes.router)
    app.include_router(maintenance_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(retention_routes.router)
    app.include_router(metrics_routes.router)
    app.include_router(user_routes.router)
    app.include_router(analytics_routes.router)
    app.include_router(worker_routes.router)
    app.include_router(queue_routes.router)
    app.include_router(workflow_routes.router)
    app.include_router(alert_routes.router)

    @app.get("/api/stream")
    def stream(request: Request, username: str = Depends(get_current_user)):
        return StreamingResponse(sse_stream(app.state.cm_event_bus), media_type="text/event-stream")

    # Fail closed: a new state-changing route that forgot `require_role`
    # would otherwise be open to every signed-in viewer. Refuse to build the
    # app instead, so the mistake surfaces in the first test run.
    missing = unclassified_write_routes(app)
    if missing:
        listed = ", ".join(f"{method} {path}" for method, path in missing)
        raise RuntimeError(f"state-changing routes without a role (use require_role): {listed}")

    static_dir = Path(__file__).parent.parent / "dashboard" / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")

    return app, session_factory
