from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from celery_manager.api.deps import get_current_user
from celery_manager.control.schedules import (
    list_periodic_schedules,
    list_upcoming_with_overrides,
    pause_occurrence,
)

router = APIRouter(prefix="/api/schedules")


class PauseRequest(BaseModel):
    target_time: datetime
    # Deliberately narrower than ScheduleOverride.action's column, which also
    # permits "reschedule". Nothing downstream implements reschedule — the
    # decorator only ever acts on "skip"/"cancel" — so accepting the value
    # here persisted an override that silently did nothing while the
    # dashboard cheerfully displayed the occurrence as paused. Rejecting it
    # at the boundary (Pydantic returns 422) is honest; storing a lie is not.
    # Widen this the day reschedule is actually implemented, not before.
    action: Literal["skip", "cancel"]


@router.post("/{name}/occurrences/pause")
def pause(name: str, payload: PauseRequest, request: Request, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    eta_tolerance_seconds = request.app.state.cm_config.eta_tolerance_seconds
    with session_factory() as session:
        override = pause_occurrence(
            session, name, payload.target_time, payload.action,
            created_by=username, eta_tolerance_seconds=eta_tolerance_seconds,
        )
    return {"id": override.id, "action": override.action, "target_time": override.target_time}


@router.get("/{name}/occurrences")
def occurrences(name: str, request: Request, username: str = Depends(get_current_user)):
    """Every upcoming occurrence for this schedule (not just already-paused
    ones) so the dashboard has something to act on — see
    list_upcoming_with_overrides in Task 6.

    Interval-schedule occurrences are anchored to the schedule's last
    observed run, so they are stable across requests once it has run at
    least once (before that they are estimates relative to "now"). Either
    way a `target_time` a user paused earlier need not be byte-identical
    to the same logical occurrence recomputed on this request;
    `list_upcoming_with_overrides` handles that itself via
    `eta_tolerance_seconds`-window matching (same concept as the
    decorator's own skip-check window) rather than exact equality — we
    just need to pass the configured tolerance through.
    """
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    eta_tolerance_seconds = request.app.state.cm_config.eta_tolerance_seconds
    with session_factory() as session:
        return list_upcoming_with_overrides(session, celery_app, name, eta_tolerance_seconds=eta_tolerance_seconds)


@router.get("")
def list_schedules(request: Request, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        schedules = list_periodic_schedules(session)
        return [
            {"name": s.name, "task_name": s.task_name, "schedule_spec": s.schedule_spec, "enabled": s.enabled}
            for s in schedules
        ]
