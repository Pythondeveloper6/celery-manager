from fastapi import APIRouter, Depends, Query, Request

from celery_manager.api.deps import get_current_user
from celery_manager.api.serialization import utc
from celery_manager.control.health import attention_runs, beat_health
from celery_manager.db.models import WorkerState

router = APIRouter(prefix="/api/health", dependencies=[Depends(get_current_user)])


@router.get("/beat")
def beat(request: Request, stale_seconds: int = Query(120, ge=30, le=86400)):
    with request.app.state.cm_session_factory() as session:
        return beat_health(session, stale_seconds=stale_seconds)


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
