"""Read-only endpoints shaped like Flower's REST API.

Scripts and Grafana JSON data sources written against Flower's
`GET /api/tasks`, `GET /api/task/info/<id>` and `GET /api/workers?status=true`
keep working when pointed at `<celery-manager>/api/flower/...` instead,
with celery-manager's authentication in front (a session cookie or the host's
bearer token, never Flower's basic auth).

Flower's most used writes are mirrored too (queue lengths, revoke,
async-apply, pool grow and shrink); they go through celery-manager's roles,
dispatch policy and audit log. Other Flower control endpoints are not offered.
Data comes from celery-manager's stored history, so tasks older than retention
are absent and `received`/`started` are the recorded timestamps.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from celery_manager.api.deps import Principal, get_current_user
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


def _task(run: TaskRun, config, role: str) -> dict:
    from celery_manager.redaction import view_payload
    args, kwargs, traceback, _, _ = view_payload(config, role, run.args, run.kwargs, run.traceback)
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
        "args": args,
        "kwargs": kwargs,
        "result": run.result_repr,
        "exception": run.exception,
        "traceback": traceback,
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
    principal: Principal,
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
        return {run.task_id: _task(run, request.app.state.cm_config, principal.role) for run in runs}


@router.get("/task/info/{task_id}")
def task_info(task_id: str, request: Request, principal: Principal, username: str = Depends(get_current_user)):
    with request.app.state.cm_session_factory() as session:
        run = session.query(TaskRun).filter(TaskRun.task_id == task_id).one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Unknown task '{task_id}'")
        return _task(run, request.app.state.cm_config, principal.role)


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


# --- Flower-shaped writes ----------------------------------------------------
# Same paths and reply shapes as Flower, mapped onto celery-manager's audited
# operations, roles and dispatch policy, so a script only changes its base URL
# and credential.

from celery_manager.api.deps import require_operator


@router.get("/queues/length")
def queues_length(request: Request, username: str = Depends(get_current_user)):
    from celery_manager.control.queues import list_queues
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        listing = list_queues(config.celery_app, session, include_workers=False)
    return {"active_queues": [{"name": q["name"], "messages": q.get("depth") or 0} for q in listing["queues"]]}


@router.post("/task/revoke/{task_id}")
def revoke(task_id: str, request: Request, principal: Principal, terminate: bool = False,
           signal: str = "SIGTERM", username: str = Depends(require_operator)):
    from celery_manager.control.tasks import cancel_task
    if signal == "SIGKILL" and principal.role != "admin":
        raise HTTPException(403, "SIGKILL requires the admin role")
    with request.app.state.cm_session_factory() as session:
        try:
            cancel_task(request.app.state.cm_config.celery_app, task_id, session=session, username=username,
                        mode="terminate" if terminate else "revoke", signal=signal.upper() if terminate else None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"message": f"Revoked '{task_id}'"}


@router.post("/task/async-apply/{task_name}")
def async_apply(task_name: str, request: Request, principal: Principal, payload: dict | None = None,
                username: str = Depends(require_operator)):
    from celery_manager.control.tasks import start_task
    payload = payload or {}
    args, kwargs = payload.get("args") or [], payload.get("kwargs") or {}
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise HTTPException(400, "args must be a list and kwargs an object")
    with request.app.state.cm_session_factory() as session:
        session.info["cm_principal"] = principal
        task_id = start_task(request.app.state.cm_config.celery_app, task_name, args, kwargs,
                             session=session, username=username, countdown=payload.get("countdown"))
    return {"task-id": task_id, "state": "PENDING"}


def _flower_pool(request: Request, worker: str, delta: int, username: str):
    from celery_manager.control.workers import WorkerControlError, resize_pool
    with request.app.state.cm_session_factory() as session:
        try:
            resize_pool(request.app.state.cm_config.celery_app, session, username, worker, delta,
                        request.app.state.cm_config.inspect_timeout_seconds)
        except WorkerControlError as exc:
            raise HTTPException(503, str(exc)) from exc
    return {"message": f"{'Growing' if delta > 0 else 'Shrinking'} '{worker}' worker's pool by {abs(delta)}"}


@router.post("/worker/pool/grow/{worker}")
def flower_pool_grow(worker: str, request: Request, n: int = Query(1, ge=1, le=100),
                     username: str = Depends(require_operator)):
    return _flower_pool(request, worker, n, username)


@router.post("/worker/pool/shrink/{worker}")
def flower_pool_shrink(worker: str, request: Request, n: int = Query(1, ge=1, le=100),
                       username: str = Depends(require_operator)):
    return _flower_pool(request, worker, -n, username)
