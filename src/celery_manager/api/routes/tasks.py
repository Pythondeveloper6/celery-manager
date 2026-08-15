from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from celery_manager.api.deps import get_current_user
from celery_manager.control.tasks import (
    list_task_runs,
    get_task_run,
    start_task,
    cancel_task,
    retry_task,
    bulk_retry,
)

router = APIRouter(prefix="/api/tasks")


@router.get("")
def list_tasks(request: Request, status: str | None = None, task_name: str | None = None, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        runs = list_task_runs(session, status=status, task_name=task_name)
        return [
            {"task_id": r.task_id, "task_name": r.task_name, "status": r.status, "started_at": r.started_at, "finished_at": r.finished_at}
            for r in runs
        ]


@router.get("/{task_id}")
def get_task(task_id: str, request: Request, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        run = get_task_run(session, task_id)
        if run is None:
            raise HTTPException(status_code=404, detail="not found")
        return {
            "task_id": run.task_id,
            "task_name": run.task_name,
            "status": run.status,
            "args": run.args,
            "kwargs": run.kwargs,
            "exception": run.exception,
            "traceback": run.traceback,
            "log_output": run.log_output,
            # Operational detail already stored on TaskRun (by the events
            # listener for worker/queue, by the decorator for the timings) but
            # never surfaced here — so the dashboard's detail view could not
            # answer "which worker ran this, off which queue, and how long did
            # it take". The list endpoint returned started_at/finished_at while
            # this one didn't, which also made the frontend's TaskDetail type
            # (extending TaskSummary) claim fields the response never sent.
            "queue": run.queue,
            "worker": run.worker,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "runtime_seconds": run.runtime_seconds,
        }


class StartRequest(BaseModel):
    task_name: str
    args: list = []
    kwargs: dict = {}


@router.post("/start")
def start(payload: StartRequest, request: Request, username: str = Depends(get_current_user)):
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    # session + username are what make this action land in the audit log,
    # same as the retry route below.
    with session_factory() as session:
        task_id = start_task(
            celery_app, payload.task_name, payload.args, payload.kwargs,
            session=session, username=username,
        )
    return {"task_id": task_id}


@router.post("/{task_id}/cancel")
def cancel(task_id: str, request: Request, terminate: bool = False, username: str = Depends(get_current_user)):
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        cancel_task(celery_app, task_id, terminate=terminate, session=session, username=username)
    return {"ok": True}


@router.post("/{task_id}/retry")
def retry(task_id: str, request: Request, username: str = Depends(get_current_user)):
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        new_id = retry_task(celery_app, session, task_id, username=username)
    return {"new_task_id": new_id}


class BulkRetryRequest(BaseModel):
    task_ids: list[str]


@router.post("/retry-bulk")
def retry_bulk(payload: BulkRetryRequest, request: Request, username: str = Depends(get_current_user)):
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        new_ids = bulk_retry(celery_app, session, payload.task_ids, username=username)
    return {"new_task_ids": new_ids}
