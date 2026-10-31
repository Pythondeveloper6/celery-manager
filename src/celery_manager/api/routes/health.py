from fastapi import APIRouter, Depends, Query, Request

from celery_manager.api.deps import get_current_user, require_admin
from celery_manager.api.serialization import utc
from celery_manager.control.health import attention_runs, beat_health
from celery_manager.db.models import WorkerState

router = APIRouter(prefix="/api/health", dependencies=[Depends(get_current_user)])


@router.get("/beat")
def beat(request: Request, stale_seconds: int = Query(120, ge=30, le=86400),
         owner_id: str | None = Query(None, min_length=1, max_length=500)):
    with request.app.state.cm_session_factory() as session:
        return beat_health(session, stale_seconds=stale_seconds, owner_id=owner_id)


@router.get("/workers")
def workers(request: Request):
    with request.app.state.cm_session_factory() as session:
        rows = session.query(WorkerState).order_by(WorkerState.last_seen_at.desc()).limit(1000).all()
        return {"items": [{"hostname": r.hostname, "state": r.state, "last_seen_at": utc(r.last_seen_at),
                           "active": r.active, "processed": r.processed} for r in rows]}


@router.get("/runs")
def runs(request: Request, stuck_seconds: int = Query(3600, ge=60, le=604800)):
    with request.app.state.cm_session_factory() as session:
        return {"items": attention_runs(session, stuck_seconds=stuck_seconds)}


@router.delete("/beat/owner")
def retire_beat_owner(request: Request, owner_id: str = Query(min_length=1, max_length=500),
                      username: str = Depends(require_admin)):
    import json

    from celery_manager.beat_health import owner_key
    from celery_manager.control.audit import record
    from celery_manager.db.models import AppSetting
    key = owner_key(owner_id)
    with request.app.state.cm_session_factory() as session:
        row = session.get(AppSetting, key)
        if row is not None:
            session.delete(row)
        legacy = session.get(AppSetting, "beat_last_tick")
        if legacy is not None:
            try:
                if json.loads(legacy.value).get("owner_id") == owner_id:
                    session.delete(legacy)
            except (ValueError, TypeError, AttributeError):
                pass
        record(session, username, "beat.retire_owner", "beat_owner", owner_id, {"removed": row is not None})
    return {"owner_id": owner_id, "removed": row is not None}
