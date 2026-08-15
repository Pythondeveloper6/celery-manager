from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from celery_manager.api.deps import get_current_user
from celery_manager.control.schedules import pause_occurrence, list_upcoming_with_overrides

router = APIRouter(prefix="/api/schedules")


class PauseRequest(BaseModel):
    target_time: datetime
    action: str  # skip | cancel | reschedule


@router.post("/{name}/occurrences/pause")
def pause(name: str, payload: PauseRequest, request: Request, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        override = pause_occurrence(session, name, payload.target_time, payload.action, created_by=username)
    return {"id": override.id, "action": override.action, "target_time": override.target_time}


@router.get("/{name}/occurrences")
def occurrences(name: str, request: Request, username: str = Depends(get_current_user)):
    """Every upcoming occurrence for this schedule (not just already-paused
    ones) so the dashboard has something to act on — see
    list_upcoming_with_overrides in Task 6.

    `list_upcoming_with_overrides` defaults `from_time` to a fresh
    `datetime.now(timezone.utc)` when none is given, computed once per
    call. For a plain (non-anchored) interval schedule that value is
    carried straight into the first computed occurrence with full
    microsecond precision, so two separate requests — each a genuinely
    separate DB session per Task 9's threadpool model — recompute
    slightly different "next occurrence" timestamps even a few
    milliseconds apart. Since overrides are matched by exact datetime
    equality (see `_as_aware_utc` in Task 6), that sub-millisecond drift
    means a target_time round-tripped from one response and echoed back
    to the pause endpoint would already mismatch the next GET's freshly
    computed occurrence — not because of the SQLite tzinfo issue Task 6
    already hardened against, but purely from two independent `now()`
    calls never landing on the same microsecond. Rounding down to whole
    seconds here (this route's own concern, not a change to Task 6's
    control-layer code) gives same-second requests — the case that
    matters for a dashboard poll/pause round-trip — a stable, identical
    anchor to compute from.
    """
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    from_time = datetime.now(timezone.utc).replace(microsecond=0)
    with session_factory() as session:
        return list_upcoming_with_overrides(session, celery_app, name, from_time=from_time)
