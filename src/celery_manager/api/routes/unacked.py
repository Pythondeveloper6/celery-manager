from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from celery_manager.api.deps import get_current_user, require_operator
from celery_manager.control.queues import QueueBrokerError
from celery_manager.control.unacked import (
    MAX_RESTORE,
    UnackedRefused,
    list_unacked,
    restore_unacked,
)

# Its own router rather than more routes on the queues one: the paths sit
# under /api/queues because unacked messages belong to queues, but the
# feature (and its Redis-only caveats) is separate. No clash with
# `POST /api/queues/{name}/purge`: different suffix, and FastAPI matches
# `GET /api/queues/unacked` here before any `{name}` route could see it.
router = APIRouter(prefix="/api/queues/unacked")


def _timeout(request: Request) -> float:
    return request.app.state.cm_config.inspect_timeout_seconds


@router.get("")
def get_unacked(request: Request, username: str = Depends(get_current_user)):
    """Messages a worker took but hasn't acknowledged (Redis transport).

    `{"supported": false, "reason": ...}` on other transports, `503` when
    Redis can't be read in time.
    """
    celery_app = request.app.state.cm_config.celery_app
    with request.app.state.cm_session_factory() as session:
        try:
            listing = list_unacked(celery_app, session, _timeout(request))
        except QueueBrokerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {**listing, "inspected_at": datetime.now(timezone.utc).isoformat()}


class RestoreRequest(BaseModel):
    tags: list[str] = Field(min_length=1, max_length=MAX_RESTORE)
    # Restore messages a live worker reports as reserved/scheduled too.
    # Never overrides the refusal for one a worker is running.
    force: bool = False


@router.post("/restore")
def restore(payload: RestoreRequest, request: Request, username: str = Depends(require_operator)):
    """Push chosen unacked messages back onto their queue. Audited.

    Operator, not admin: nothing is deleted (the message goes back to where
    it came from, which is what kombu itself does after the visibility
    timeout) and the risk — a duplicate run — is refused for anything a live
    worker reports holding. `409` if the workers can't be asked (nothing is
    restored then), `503` for broker trouble.
    """
    celery_app = request.app.state.cm_config.celery_app
    with request.app.state.cm_session_factory() as session:
        try:
            return restore_unacked(celery_app, session, username, payload.tags, _timeout(request), force=payload.force)
        except UnackedRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except QueueBrokerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
