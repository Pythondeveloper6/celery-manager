from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from celery_manager.api.deps import get_current_user, require_admin, require_operator
from celery_manager.control.workers import (
    MAX_NAME_LENGTH,
    WorkerControlError,
    inspect_workers,
    list_task_rate_limits,
    resize_pool,
    set_consumer,
    set_rate_limit,
    shutdown_worker,
)

# No prefix: `POST /api/tasks/rate-limit` belongs to the workers feature (it
# is a broadcast to workers, not an operation on a stored run) but reads as a
# task operation, so its path says so. The tasks router has no POST route
# with a single path segment after /api/tasks that it could collide with.
router = APIRouter()

Name = Field(min_length=1, max_length=MAX_NAME_LENGTH)


def _broker_unavailable(exc: WorkerControlError) -> HTTPException:
    # 503, not 500: the dashboard is fine, the broker isn't. The message
    # says what to check rather than carrying a stack trace.
    return HTTPException(status_code=503, detail=str(exc))


def _timeout(request: Request) -> float:
    return request.app.state.cm_config.inspect_timeout_seconds


@router.get("/api/workers")
def list_workers(request: Request, username: str = Depends(get_current_user)):
    """Live snapshot of every worker that answers within the configured
    inspect timeout. An empty `workers` list means none replied — not
    necessarily that none exist (see docs/workers.md)."""
    celery_app = request.app.state.cm_config.celery_app
    timeout = _timeout(request)
    try:
        workers = inspect_workers(celery_app, timeout)
    except WorkerControlError as exc:
        raise _broker_unavailable(exc) from exc
    return {
        "workers": workers,
        "timeout_seconds": timeout,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/workers/rate-limits")
def list_rate_limits(request: Request, username: str = Depends(get_current_user)):
    return list_task_rate_limits(request.app.state.cm_config.celery_app)


class PoolRequest(BaseModel):
    n: int = Field(default=1, ge=1, le=64)


class QueueRequest(BaseModel):
    queue: str = Name


def _run(request: Request, fn, *args, **kwargs):
    celery_app = request.app.state.cm_config.celery_app
    with request.app.state.cm_session_factory() as session:
        try:
            return fn(celery_app, session, *args, **kwargs)
        except WorkerControlError as exc:
            raise _broker_unavailable(exc) from exc


def _check_worker_name(worker: str) -> None:
    if not worker or len(worker) > MAX_NAME_LENGTH:
        raise HTTPException(status_code=422, detail="Invalid worker name.")


@router.post("/api/workers/{worker}/shutdown")
def shutdown(worker: str, request: Request, username: str = Depends(require_admin)):
    _check_worker_name(worker)
    return _run(request, shutdown_worker, username, worker)


@router.post("/api/workers/{worker}/pool/grow")
def pool_grow(worker: str, request: Request, payload: PoolRequest | None = None, username: str = Depends(require_operator)):
    _check_worker_name(worker)
    n = payload.n if payload else 1
    return _run(request, resize_pool, username, worker, n, _timeout(request))


@router.post("/api/workers/{worker}/pool/shrink")
def pool_shrink(worker: str, request: Request, payload: PoolRequest | None = None, username: str = Depends(require_operator)):
    _check_worker_name(worker)
    n = payload.n if payload else 1
    return _run(request, resize_pool, username, worker, -n, _timeout(request))


@router.post("/api/workers/{worker}/queues/add")
def add_consumer(worker: str, payload: QueueRequest, request: Request, username: str = Depends(require_operator)):
    _check_worker_name(worker)
    return _run(request, set_consumer, username, worker, payload.queue, True, _timeout(request))


@router.post("/api/workers/{worker}/queues/cancel")
def cancel_consumer(worker: str, payload: QueueRequest, request: Request, username: str = Depends(require_operator)):
    _check_worker_name(worker)
    return _run(request, set_consumer, username, worker, payload.queue, False, _timeout(request))


class RateLimitRequest(BaseModel):
    task_name: str = Name
    # "" clears the limit; the format is checked in control.workers so a
    # Python caller gets the same validation as the API.
    rate_limit: str = Field(max_length=32)
    destination: list[str] | None = None


@router.post("/api/tasks/rate-limit")
def rate_limit(payload: RateLimitRequest, request: Request, username: str = Depends(require_operator)):
    destination = [d for d in payload.destination or [] if d] or None
    if destination and any(len(d) > MAX_NAME_LENGTH for d in destination):
        raise HTTPException(status_code=422, detail="Invalid worker name.")
    try:
        return _run(
            request, set_rate_limit, username, payload.task_name, payload.rate_limit.strip(),
            _timeout(request), destination,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/workers/{worker}/heartbeats")
def heartbeat_history(worker: str, request: Request,
                      since: datetime | None = None, until: datetime | None = None,
                      limit: int = Query(120, ge=1, le=1000), offset: int = Query(0, ge=0, le=1000000),
                      username: str = Depends(get_current_user)):
    from celery_manager.api.serialization import utc, utc_bound
    from celery_manager.control.worker_history import list_heartbeats
    _check_worker_name(worker)
    since, until = utc_bound(since), utc_bound(until)
    if since is not None and until is not None and since > until:
        raise HTTPException(422, "since must be before until")
    with request.app.state.cm_session_factory() as session:
        rows,total = list_heartbeats(session,worker,since=since,until=until,limit=limit,offset=offset)
        return {"items":[{"bucket_at":utc(r.bucket_at),"observed_at":utc(r.observed_at),"state":r.state,
                          "active":r.active,"processed":r.processed} for r in rows],
                "total":total,"limit":limit,"offset":offset,
                "retention_days":request.app.state.cm_config.worker_heartbeat_retention_days}
