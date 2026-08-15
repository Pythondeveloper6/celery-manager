from __future__ import annotations

from datetime import datetime

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

    `list_upcoming_with_overrides` recomputes occurrence timestamps
    relative to "now" on every call, so a `target_time` a user paused a
    few seconds ago won't be byte-identical to the same logical
    occurrence recomputed on this request. It handles that itself via
    `eta_tolerance_seconds`-window matching (same concept as the
    decorator's own skip-check window) rather than exact equality — we
    just need to pass the configured tolerance through.
    """
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    eta_tolerance_seconds = request.app.state.cm_config.eta_tolerance_seconds
    with session_factory() as session:
        return list_upcoming_with_overrides(session, celery_app, name, eta_tolerance_seconds=eta_tolerance_seconds)
