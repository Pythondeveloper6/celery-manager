"""Opt-in, request-only tenant history isolation; no global operational access."""
from __future__ import annotations

import inspect
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.orm import with_loader_criteria
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from celery_manager.db.identity import exact_identity, portable_text_fits


@dataclass(frozen=True)
class TenantScope:
    engine: object
    tenant_id: str


_scope: ContextVar[TenantScope | None] = ContextVar("cm_request_tenant", default=None)


def current_tenant() -> str | None:
    scope = _scope.get()
    return scope.tenant_id if scope else None


def install_tenant_isolation(app, session_factory, config):
    """Attach criteria to this factory only. Worker sessions outside requests stay global."""
    if config.tenant_resolver is None:
        return
    if not callable(config.tenant_resolver):
        raise TypeError("tenant_resolver must be a trusted host callable")
    from celery_manager.db.models import (
        FailureResolution,
        FailureResolutionHistory,
        TaskRun,
    )
    engine = session_factory.kw["bind"]

    def scope_query(execution):
        scope = _scope.get()
        if scope is None or scope.engine is not engine or execution.session.get_bind() is not engine:
            return
        tenant_id = scope.tenant_id
        # No tenant operational writes are admitted by the HTTP boundary.
        # The ORM criterion is also applied to aggregates/column-only queries,
        # aliases and nested subqueries before LIMIT/OFFSET are evaluated.
        if execution.is_select:
            visible_ids = select(TaskRun.task_id).where(exact_identity(TaskRun.tenant_id, tenant_id))
            execution.statement = execution.statement.options(
                with_loader_criteria(TaskRun, exact_identity(TaskRun.tenant_id, tenant_id), include_aliases=True),
                with_loader_criteria(FailureResolution, FailureResolution.task_id.in_(visible_ids), include_aliases=True),
                with_loader_criteria(FailureResolutionHistory, FailureResolutionHistory.task_id.in_(visible_ids), include_aliases=True),
            )
        elif execution.is_update or execution.is_delete:
            raise HTTPException(403, "Tenant history access is read-only")

    event.listen(session_factory, "do_orm_execute", scope_query)
    app.add_middleware(TenantHistoryMiddleware, config=config, engine=engine, owner=app)


# Fail closed: new routes require an explicit security review before admission.
_PUBLIC = {("GET", "/api/auth/options"), ("POST", "/api/auth/login"), ("POST", "/api/auth/logout")}


def _path(scope):
    path = scope.get("path", "")
    root = scope.get("root_path", "")
    if root and path.startswith(root + "/"):
        path = path[len(root):]
    if path.startswith("/api/v1/"):
        path = "/api/" + path[len("/api/v1/"):]
    return path.rstrip("/") or "/"


class TenantHistoryMiddleware:
    def __init__(self, app, *, config, engine, owner):
        self.app, self.config, self.engine, self.owner = app, config, engine, owner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # No alternate live channel is permitted to bypass scope checks.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        path, method = _path(scope), scope["method"]
        if not path.startswith("/api") and path not in {"/metrics", "/openapi.json", "/docs", "/redoc"}:
            await self.app(scope, receive, send)
            return
        if (method, path) in _PUBLIC:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        try:
            from celery_manager.api.deps import SESSION_COOKIE, get_current_principal
            principal = await run_in_threadpool(get_current_principal, request, request.cookies.get(SESSION_COOKIE))
            resolver = self.config.tenant_resolver
            if inspect.iscoroutinefunction(resolver):
                tenant = await resolver(request, principal)
            else:
                tenant = await run_in_threadpool(resolver, request, principal)
                if inspect.isawaitable(tenant):
                    tenant = await tenant
            if not portable_text_fits(tenant, 255) or not tenant.strip() or not tenant.isprintable():
                raise HTTPException(403, "A trusted tenant scope is required")
            request.state.cm_tenant_principal = principal
            request.state.cm_tenant_id = tenant
            from starlette.routing import compile_path

            from celery_manager.api.app import iter_api_routes
            reviewed = {"/api/whoami", "/api/tasks", "/api/tasks/export", "/api/tasks/{task_id}",
                        "/api/tasks/{task_id}/workflow", "/api/tasks/{task_id}/related",
                        "/api/dead-letters", "/api/dead-letters/{task_id}/history"}
            matched = None
            for template, route, _ in iter_api_routes(self.owner.routes):
                if template.startswith("/api/v1/") or method not in route.methods:
                    continue
                if compile_path(template)[0].fullmatch(path):
                    matched = template
                    break
            allowed = method == "GET" and matched in reviewed
            if not allowed:
                raise HTTPException(403, "Tenant history mode denies global views and operational writes")
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
            return
        except Exception:  # noqa: BLE001 - resolver failures must fail closed
            # Resolver bugs/outages must never fall back to global visibility.
            await JSONResponse({"detail": "Tenant scope could not be resolved"}, status_code=503)(scope, receive, send)
            return
        token = _scope.set(TenantScope(self.engine, tenant))
        try:
            await self.app(scope, receive, send)
        finally:
            _scope.reset(token)
