from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from celery_manager.api.deps import get_current_user
from celery_manager.api.serialization import utc, utc_bound
from celery_manager.control.tasks import (
    MAX_PAGE_SIZE,
    TaskRunNotFound,
    bulk_retry,
    cancel_task,
    count_task_runs,
    get_task_run,
    list_registered_tasks,
    list_retries_of,
    list_task_runs,
    retry_task,
    start_task,
)

router = APIRouter(prefix="/api/tasks")


@router.get("")
def list_tasks(
    request: Request,
    status: str | None = None,
    task_name: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    username: str = Depends(get_current_user),
):
    """One page of task runs, plus the total matching the same filters.

    Returns an envelope (`items`/`total`/`limit`/`offset`) rather than a bare
    array. The array shape had no room for a total, which left the UI unable
    to tell "50 results" from "the first 50 of 1,284" — and with the old hard
    50-row cap and no offset, older history was simply unreachable. Changing
    the shape is a deliberate break, taken while the package is unreleased at
    0.1.0 and its only consumers are our own SPA and docs/rest-api.md.
    """
    # Clamp rather than reject: `?limit=10000000` is more often a stale
    # bookmark than an attack, but either way it must not turn one HTTP
    # request into a full-table load, and a 400 here would only give the
    # caller a broken page instead of a capped one. Negative values are
    # meaningless to OFFSET/LIMIT, so floor them too.
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)

    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        filters = {
            "status": status, "task_name": task_name, "q": q,
            "date_from": utc_bound(date_from), "date_to": utc_bound(date_to),
        }
        # The same filter dict feeds both calls, so `total` can never be a
        # count of some other query than the one `items` came from.
        runs = list_task_runs(session, limit=limit, offset=offset, **filters)
        total = count_task_runs(session, **filters)
        return {
            "items": [
                {
                    "task_id": r.task_id,
                    "task_name": r.task_name,
                    "status": r.status,
                    "started_at": utc(r.started_at),
                    "finished_at": utc(r.finished_at),
                    "runtime_seconds": r.runtime_seconds,
                    # Progress is opt-in (only tasks that report it have any),
                    # so these are usually null. Shipped raw: the frontend
                    # derives percentage and ETA, because it is the side that
                    # knows how much space it has to say it in.
                    "progress_current": r.progress_current,
                    "progress_total": r.progress_total,
                    "progress_message": r.progress_message,
                    "progress_updated_at": utc(r.progress_updated_at),
                }
                for r in runs
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/registered")
def registered(request: Request, username: str = Depends(get_current_user)):
    """Task names registered on the Celery app, for the dashboard's task picker."""
    return {"items": list_registered_tasks(request.app.state.cm_config.celery_app)}


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
            "eta": utc(run.eta),
            "started_at": utc(run.started_at),
            "finished_at": utc(run.finished_at),
            "runtime_seconds": run.runtime_seconds,
            "progress_current": run.progress_current,
            "progress_total": run.progress_total,
            "progress_message": run.progress_message,
            "progress_updated_at": utc(run.progress_updated_at),
            # Both directions of the retry chain: the run this one was
            # retried from, and the runs retried from it. `retried_by` is a
            # list because an operator can retry the same failed run more than
            # once, and each attempt is its own run.
            "retried_from_id": run.retried_from_id,
            "retried_by": [r.task_id for r in list_retries_of(session, task_id)],
            # Workflow lineage (chain/group/chord); see GET /{task_id}/workflow.
            "parent_id": run.parent_id,
            "root_id": run.root_id,
            "group_id": run.group_id,
        }


def _aware(value: datetime | None) -> datetime | None:
    # Celery reads a naive eta in the app timezone; this codebase's convention
    # is that naive means UTC, so say so explicitly.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class StartRequest(BaseModel):
    task_name: str
    args: list = []
    kwargs: dict = {}
    queue: str | None = None
    countdown: float | None = Field(default=None, ge=0)
    eta: datetime | None = None


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
            queue=payload.queue, countdown=payload.countdown, eta=_aware(payload.eta),
        )
    return {"task_id": task_id}


@router.post("/{task_id}/cancel")
def cancel(task_id: str, request: Request, terminate: bool = False, username: str = Depends(get_current_user)):
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        cancel_task(celery_app, task_id, terminate=terminate, session=session, username=username)
    return {"ok": True}


class RetryRequest(BaseModel):
    """Optional overrides; a field left out keeps the stored value."""

    args: list | None = None
    kwargs: dict | None = None


@router.post("/{task_id}/retry")
def retry(
    task_id: str,
    request: Request,
    payload: RetryRequest | None = None,
    username: str = Depends(get_current_user),
):
    """Re-dispatch a stored run. 404 if there is no such run.

    A retry of an unknown task id used to escape as an unhandled `ValueError`
    and reach the client as a 500 — which reads as "the dashboard is broken"
    rather than "that task id doesn't exist", and is indistinguishable from a
    genuine server fault to anyone reading logs or alerting on 5xx. It is the
    same condition `GET /api/tasks/{task_id}` already answers with a 404, so
    it gets the same status. Only `TaskRunNotFound` is translated: a
    `ValueError` from undecodable stored args is a real server-side problem
    and must keep surfacing as one.
    """
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        try:
            new_id = retry_task(
                celery_app, session, task_id, username=username,
                args=payload.args if payload else None,
                kwargs=payload.kwargs if payload else None,
            )
        except TaskRunNotFound:
            raise HTTPException(status_code=404, detail="not found")
    return {"new_task_id": new_id}


class BulkRetryRequest(BaseModel):
    task_ids: list[str]


@router.post("/retry-bulk")
def retry_bulk(payload: BulkRetryRequest, request: Request, username: str = Depends(get_current_user)):
    """Re-dispatch several stored runs. 404 if any of them is unknown.

    `bulk_retry` stops at the first unknown id (documented limitation — it is
    not partial-failure aware), so an unknown id in the batch is reported the
    same way a single unknown id is, rather than as a 500.
    """
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        try:
            new_ids = bulk_retry(celery_app, session, payload.task_ids, username=username)
        except TaskRunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
    return {"new_task_ids": new_ids}
