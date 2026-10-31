"""Read-only endpoints shaped like Flower's REST API.

Scripts and Grafana JSON data sources written against Flower's
`GET /api/tasks`, `GET /api/task/info/<id>` and `GET /api/workers?status=true`
keep working when pointed at `<celery-manager>/api/flower/...` instead,
with celery-manager's authentication in front (a session cookie or the host's
bearer token, never Flower's basic auth).

Only reads are offered. Flower's control endpoints (revoke, rate limit, pool
resize, ...) are not: use celery-manager's audited `/api/v1` routes for those.
Data comes from celery-manager's stored history, so tasks older than retention
are absent and `received`/`started` are the recorded timestamps.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from celery_manager.api.deps import get_current_user
from celery_manager.db.models import TaskRun, WorkerState

router = APIRouter(prefix="/api/flower")

MAX_LIMIT = 1000

# celery-manager status -> Celery state name, as Flower reports it.
_STATE = {
    "queued": "PENDING", "received": "RECEIVED", "started": "STARTED", "succeeded": "SUCCESS",
    "failed": "FAILURE", "retried": "RETRY", "revoked": "REVOKED", "skipped": "REVOKED",
    "interrupted": "FAILURE",
}
_STATUS = {}
for _status, _state in _STATE.items():
    _STATUS.setdefault(_state, []).append(_status)

_SORT = {"name": TaskRun.task_name, "state": TaskRun.status, "received": TaskRun.received_at,
         "started": TaskRun.started_at}


def _epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _task(run: TaskRun) -> dict:
    state = _STATE.get(run.status, run.status.upper())
    return {
        "uuid": run.task_id,
        "name": run.task_name,
        "state": state,
        "received": _epoch(run.received_at or run.sent_at),
        "sent": _epoch(run.sent_at),
        "started": _epoch(run.started_at),
        "succeeded": _epoch(run.finished_at) if state == "SUCCESS" else None,
        "failed": _epoch(run.finished_at) if state == "FAILURE" else None,
        "revoked": _epoch(run.finished_at) if state == "REVOKED" else None,
        "timestamp": _epoch(run.finished_at or run.started_at or run.received_at or run.sent_at),
        "args": run.args,
        "kwargs": run.kwargs,
        "result": run.result_repr,
        "exception": run.exception,
        "traceback": run.traceback,
        "retries": run.retry_count,
        "runtime": run.runtime_seconds,
        "worker": run.worker,
        "routing_key": run.queue,
        "eta": run.eta.isoformat() if run.eta else None,
        "parent_id": run.parent_id,
        "root_id": run.root_id,
    }


def _since(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(status_code=400, detail=f"{name} must be ISO 8601 or a Unix timestamp") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@router.get("/tasks")
def tasks(
    request: Request,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    sort_by: str | None = None,
    workername: str | None = None,
    taskname: str | None = None,
    state: str | None = None,
    received_start: str | None = None,
    received_end: str | None = None,
    username: str = Depends(get_current_user),
):
    """`{task_id: task}`, newest first unless `sort_by` (prefix `-` for descending)."""
    limit = min(limit, MAX_LIMIT)
    with request.app.state.cm_session_factory() as session:
        query = session.query(TaskRun)
        if workername:
            query = query.filter(TaskRun.worker == workername)
        if taskname:
            query = query.filter(TaskRun.task_name == taskname)
        if state:
            statuses = _STATUS.get(state.upper())
            if statuses is None:
                raise HTTPException(status_code=400, detail=f"unknown state {state!r}")
            query = query.filter(TaskRun.status.in_(statuses))
        start, end = _since(received_start, "received_start"), _since(received_end, "received_end")
        if start:
            query = query.filter(TaskRun.received_at >= start)
        if end:
            query = query.filter(TaskRun.received_at <= end)
        order = TaskRun.id.desc()
        if sort_by:
            column = _SORT.get(sort_by.lstrip("-"))
            if column is None:
                raise HTTPException(status_code=400, detail=f"sort_by must be one of {', '.join(sorted(_SORT))}")
            order = column.desc() if sort_by.startswith("-") else column.asc()
        runs = query.order_by(order, TaskRun.id.desc()).offset(offset).limit(limit).all()
        return {run.task_id: _task(run) for run in runs}


@router.get("/task/info/{task_id}")
def task_info(task_id: str, request: Request, username: str = Depends(get_current_user)):
    with request.app.state.cm_session_factory() as session:
        run = session.query(TaskRun).filter(TaskRun.task_id == task_id).one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Unknown task '{task_id}'")
        return _task(run)


@router.get("/workers")
def workers(request: Request, status: bool = False, username: str = Depends(get_current_user)):
    """`{hostname: online}` with `?status=true` (Flower's cheap form); otherwise
    `{hostname: {...}}` with the last observed state. Live `inspect` data
    (registered tasks, pool stats) is on `/api/v1/workers`."""
    with request.app.state.cm_session_factory() as session:
        rows = session.query(WorkerState).order_by(WorkerState.hostname).all()
        if status:
            return {row.hostname: row.state == "online" for row in rows}
        return {row.hostname: {"status": row.state == "online", "state": row.state, "active": row.active,
                               "processed": row.processed, "last_seen": _epoch(row.last_seen_at)} for row in rows}
