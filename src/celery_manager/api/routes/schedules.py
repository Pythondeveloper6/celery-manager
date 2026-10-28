from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from celery_manager.api.deps import get_current_user, require_operator
from celery_manager.api.serialization import utc
from celery_manager.control.missed import (
    first_stamped_occurrence,
    list_missed_runs,
    missed_run_counts,
)
from celery_manager.control.schedules import (
    is_paused,
    list_periodic_schedules,
    list_upcoming_with_overrides,
    pause_occurrence,
    pause_schedule,
    resume_schedule,
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
def pause(name: str, payload: PauseRequest, request: Request, username: str = Depends(require_operator)):
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


@router.get("/{name}/missed")
def missed(name: str, request: Request, username: str = Depends(get_current_user)):
    """Scheduled runs of this schedule that were due but never arrived (last
    7 days, newest detection rules in `control/missed.py`). `active` is false
    until beat has stamped a run for it, i.e. detection is not yet possible."""
    config = request.app.state.cm_config
    if name not in (config.celery_app.conf.beat_schedule or {}):
        raise HTTPException(status_code=404, detail="Unknown schedule.")
    with request.app.state.cm_session_factory() as session:
        tracking_since = first_stamped_occurrence(session, name)
        runs = list_missed_runs(
            session, config.celery_app, name, grace_seconds=config.missed_run_grace_seconds
        )
    return {
        "active": tracking_since is not None,
        "tracking_since": utc(tracking_since),
        "grace_seconds": config.missed_run_grace_seconds,
        "missed": [{"due_at": utc(r["due_at"])} for r in runs],
    }


def _schedule_summary(s) -> dict:
    return {
        "name": s.name,
        "task_name": s.task_name,
        "schedule_spec": s.schedule_spec,
        "enabled": s.enabled,
        "paused": is_paused(s),
        "paused_at": utc(s.paused_at),
        "paused_by": s.paused_by,
        "paused_until": utc(s.paused_until),
    }


class PauseScheduleRequest(BaseModel):
    # Optional auto-resume time. Naive = UTC. Must be in the future (422 otherwise).
    until: datetime | None = None


@router.post("/{name}/pause")
def pause_whole_schedule(
    name: str,
    request: Request,
    payload: PauseScheduleRequest | None = None,
    username: str = Depends(require_operator),
):
    """Pause every firing of this schedule until resumed, or until the
    optional body `{"until": <iso>}`. Idempotent: repeating the pause keeps
    the original `paused_at`/`paused_by`; a different `until` only moves the
    end time."""
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        try:
            row = pause_schedule(session, name, username, until=payload.until if payload else None)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="`until` must be a time in the future.") from exc
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown schedule.")
        return _schedule_summary(row)


@router.post("/{name}/resume")
def resume_whole_schedule(name: str, request: Request, username: str = Depends(require_operator)):
    """Resume a paused schedule. The next regular beat firing runs; nothing
    runs immediately and missed firings are not caught up. Idempotent."""
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        row = resume_schedule(session, name, username)
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown schedule.")
        return _schedule_summary(row)


@router.get("")
def list_schedules(request: Request, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    config = request.app.state.cm_config
    with session_factory() as session:
        counts = missed_run_counts(
            session,
            config.celery_app,
            datetime.now(timezone.utc) - timedelta(hours=24),
            grace_seconds=config.missed_run_grace_seconds,
        )
        return [
            {**_schedule_summary(s), "missed_count": counts.get(s.name, 0)}
            for s in list_periodic_schedules(session)
        ]
