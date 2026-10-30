"""Persisted host health; providers never execute in an HTTP request."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from celery_manager.api.deps import require_admin, require_role
from celery_manager.control.audit import record
from celery_manager.health_probes import HealthScopeUnavailable, probe_status
from celery_manager.scheduler_health import retire_scheduler, scheduler_status

router = APIRouter(prefix="/api/health")
require_viewer = require_role("viewer")


@router.get("/probes")
def probes(request: Request, username: str = Depends(require_viewer)):
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        try:
            return {"items": probe_status(session, freshness_seconds=getattr(config, "probe_freshness_seconds", 120),
                                          expected_names=[item.name for item in getattr(config, "health_probes", ())])}
        except HealthScopeUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc


@router.get("/schedulers")
def schedulers(request: Request, username: str = Depends(require_viewer)):
    with request.app.state.cm_session_factory() as session:
        try:
            return {"items": scheduler_status(session)}
        except HealthScopeUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc


@router.delete("/schedulers/{instance_id}")
def retire(instance_id: Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}\z")], request: Request, username: str = Depends(require_admin)):
    with request.app.state.cm_session_factory() as session:
        record(session, username, "scheduler.retire_requested", "scheduler", instance_id)
        removed = retire_scheduler(session, instance_id)
        record(session, username, "scheduler.retired", "scheduler", instance_id, {"removed": removed})
    return {"removed": removed}
