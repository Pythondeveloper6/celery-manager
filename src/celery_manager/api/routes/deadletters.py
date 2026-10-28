from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from celery_manager.api.deps import Principal, get_current_user, require_operator
from celery_manager.api.serialization import utc, utc_bound
from celery_manager.control.deadletter import (
    BulkRerunTooLarge,
    MixedTaskNames,
    NotDeadLetter,
    earlier_attempts,
    exception_type,
    last_traceback_line,
    list_dead_letters,
    rerun_with_arguments,
    summarize_dead_letters,
)
from celery_manager.control.policy import DispatchDenied
from celery_manager.control.tasks import MAX_PAGE_SIZE, TaskRunNotFound

router = APIRouter(prefix="/api/dead-letters")


@router.get("")
def list_dead_letter_runs(
    request: Request,
    task_name: str | None = None,
    exception: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    username: str = Depends(get_current_user),
):
    """One page of dead letters plus a summary by task and exception type.

    The envelope matches `GET /api/tasks` (`items`/`total`/`limit`/`offset`),
    with `summary` added. `exception` is an exception type name. The summary
    ignores `task_name` and `exception` on purpose; see `summarize_dead_letters`.
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)
    since = utc_bound(since)
    with request.app.state.cm_session_factory() as session:
        runs, total = list_dead_letters(
            session, task_name=task_name, exception=exception, since=since, limit=limit, offset=offset,
        )
        attempts = earlier_attempts(session, runs)
        return {
            "items": [
                {
                    "task_id": r.task_id,
                    "task_name": r.task_name,
                    "exception": r.exception,
                    "exception_type": exception_type(r),
                    "last_line": last_traceback_line(r),
                    "args": r.args,
                    "kwargs": r.kwargs,
                    "queue": r.queue,
                    "earlier_attempts": attempts.get(r.task_id, 0),
                    "started_at": utc(r.started_at),
                    "finished_at": utc(r.finished_at),
                }
                for r in runs
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "summary": summarize_dead_letters(session, since=since),
        }


class RerunRequest(BaseModel):
    """`args`/`kwargs` left out keep each run's stored value. Typed so a
    wrong shape (an object for args, a list for kwargs) is a 422 before the
    control layer sees it."""

    task_ids: list[str] = Field(min_length=1)
    args: list | None = None
    kwargs: dict | None = None


@router.post("/rerun")
def rerun(payload: RerunRequest, request: Request, principal: Principal, username: str = Depends(require_operator)):
    """Run dead letters again, with their own arguments or with edited ones.

    404 for an unknown id, 409 if a selected run is no longer a dead letter
    (somebody re-ran it first), 400 for too many runs or edited arguments
    across several tasks, 422 for stored arguments that cannot be decoded.
    Every refusal happens before anything is dispatched.
    """
    celery_app = request.app.state.cm_config.celery_app
    with request.app.state.cm_session_factory() as session:
        try:
            session.info["cm_principal"] = principal
            return rerun_with_arguments(
                celery_app, session, payload.task_ids, payload.args, payload.kwargs, username,
            )
        except TaskRunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except NotDeadLetter as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (BulkRerunTooLarge, MixedTaskNames) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except DispatchDenied:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
