from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from celery_manager.api.deps import get_current_user
from celery_manager.control.tasks import (
    MAX_PAGE_SIZE,
    list_task_runs,
    count_task_runs,
    get_task_run,
    start_task,
    cancel_task,
    retry_task,
    bulk_retry,
    list_retries_of,
)

router = APIRouter(prefix="/api/tasks")


def _utc(value: datetime | None) -> datetime | None:
    """Stamp UTC onto a datetime read back from the database.

    SQLite drops tzinfo on a `DateTime(timezone=True)` round-trip, so
    `run.started_at` comes back naive even though it was written as an aware
    UTC value (every writer in this codebase uses `datetime.now(timezone.utc)`
    — see `decorator._find_matching_override` for the same normalization on
    the read side). FastAPI then serializes the naive value as
    "2026-08-10T00:00:00", with no offset marker — and `new Date(...)` on that
    string in a browser parses it as *local* time. For a viewer in UTC+2 that
    shifts every timestamp by two hours: today it only skews the "Started"
    column, but a live elapsed-time counter fed from it would read two hours
    wrong, or negative.

    So normalize at the API boundary, where the value becomes JSON, rather
    than in each caller. None passes through untouched — a run that never
    started has no start time, and inventing one would be worse than omitting
    it.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


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
            "date_from": date_from, "date_to": date_to,
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
                    "started_at": _utc(r.started_at),
                    "finished_at": _utc(r.finished_at),
                    "runtime_seconds": r.runtime_seconds,
                    # Progress is opt-in (only tasks that report it have any),
                    # so these are usually null. Shipped raw: the frontend
                    # derives percentage and ETA, because it is the side that
                    # knows how much space it has to say it in.
                    "progress_current": r.progress_current,
                    "progress_total": r.progress_total,
                    "progress_message": r.progress_message,
                    "progress_updated_at": _utc(r.progress_updated_at),
                }
                for r in runs
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


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
            "eta": _utc(run.eta),
            "started_at": _utc(run.started_at),
            "finished_at": _utc(run.finished_at),
            "runtime_seconds": run.runtime_seconds,
            "progress_current": run.progress_current,
            "progress_total": run.progress_total,
            "progress_message": run.progress_message,
            "progress_updated_at": _utc(run.progress_updated_at),
            # Both directions of the retry chain: the run this one was
            # retried from, and the runs retried from it. `retried_by` is a
            # list because an operator can retry the same failed run more than
            # once, and each attempt is its own run.
            "retried_from_id": run.retried_from_id,
            "retried_by": [r.task_id for r in list_retries_of(session, task_id)],
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
