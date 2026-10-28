from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from celery_manager.api.deps import Principal, require_operator
from celery_manager.auth.roles import role_allows
from celery_manager.control.tasks import MAX_BULK_CANCEL, BulkCancelTooLarge
from celery_manager.control.workers import (
    WorkerControlError,
    cancel_delayed_tasks,
    list_delayed_tasks,
)

router = APIRouter(prefix="/api/delayed")


def _timeout(request: Request) -> float:
    return request.app.state.cm_config.inspect_timeout_seconds


@router.get("")
def get_delayed(request: Request, principal: Principal, reveal_args: bool = False):
    """ETA/countdown tasks held by live workers. Any signed-in role.

    Arguments are masked unless `reveal_args=true`, which needs the
    operator role: the values come straight out of a worker's memory, and
    the shape (types and keyword names) is enough to tell calls apart.
    """
    if reveal_args and not role_allows(principal.role, "operator"):
        raise HTTPException(
            status_code=403,
            detail=f"Not allowed: showing task arguments needs the operator role (you are {principal.role}).",
        )
    celery_app = request.app.state.cm_config.celery_app
    timeout = _timeout(request)
    with request.app.state.cm_session_factory() as session:
        try:
            listing = list_delayed_tasks(celery_app, session, timeout, reveal_args=reveal_args)
        except WorkerControlError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        **listing,
        "args_revealed": reveal_args,
        "timeout_seconds": timeout,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }


class CancelDelayedRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=MAX_BULK_CANCEL)


@router.post("/cancel")
def cancel_delayed(payload: CancelDelayedRequest, request: Request, username: str = Depends(require_operator)):
    """Revoke one or more delayed tasks and mark their runs `revoked` now.

    Not `POST /api/tasks/{id}/cancel` or `cancel-bulk`: those leave the run
    for the events listener to mark, which for an ETA task only happens at
    the ETA, and bulk cancel skips ids with no recorded run. See
    `cancel_delayed_tasks`.
    """
    celery_app = request.app.state.cm_config.celery_app
    with request.app.state.cm_session_factory() as session:
        try:
            return cancel_delayed_tasks(celery_app, session, username, payload.task_ids)
        except WorkerControlError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (BulkCancelTooLarge, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
