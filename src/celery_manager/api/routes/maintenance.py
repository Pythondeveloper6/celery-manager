from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from celery_manager.api.deps import get_current_user, require_operator, require_role
from celery_manager.api.serialization import utc
from celery_manager.control.maintenance import (
    DuplicateWindowName,
    MaintenanceWindowError,
    create_window,
    default_timezone,
    delete_window,
    list_windows,
    next_spans,
    normalize_recurrence,
    update_window,
    window_fields,
)

router = APIRouter(prefix="/api/maintenance-windows")

PREVIEW_COUNT = 5


def _known_schedules(request: Request) -> set[str]:
    return set(request.app.state.cm_config.celery_app.conf.beat_schedule or {})


@router.get("")
def list_all(request: Request, username: str = Depends(get_current_user)):
    """Every window, with whether it is in progress and its next span, plus
    the timezone a new window defaults to (the Celery app's)."""
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        return {
            "default_timezone": default_timezone(config.celery_app),
            "windows": [window_fields(w) for w in list_windows(session)],
        }


@router.post("/preview")
def preview(request: Request, body: Annotated[dict, Body()], username: str = Depends(require_role("viewer"))):
    """The next five spans (UTC start/end) of the recurrence in the body
    (`days_of_week`, `start_time`, `duration_minutes`, `timezone`), without
    saving anything. A POST only because of the body; any signed-in account
    may call it. 422 with a sentence for invalid fields."""
    try:
        fields = normalize_recurrence(body, default_timezone(request.app.state.cm_config.celery_app))
    except MaintenanceWindowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    spans = next_spans(SimpleNamespace(**fields), datetime.now(timezone.utc), PREVIEW_COUNT)
    return {"windows": [{"start": utc(a), "end": utc(b)} for a, b in spans]}


@router.post("")
def create(request: Request, body: Annotated[dict, Body()], username: str = Depends(require_operator)):
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        try:
            row = create_window(
                session, body, username, default_timezone(config.celery_app), _known_schedules(request)
            )
        except MaintenanceWindowError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DuplicateWindowName as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return window_fields(row)


@router.put("/{window_id}")
def update(window_id: int, request: Request, body: Annotated[dict, Body()], username: str = Depends(require_operator)):
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        try:
            row = update_window(
                session, window_id, body, username, default_timezone(config.celery_app), _known_schedules(request)
            )
        except MaintenanceWindowError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DuplicateWindowName as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown maintenance window.")
        return window_fields(row)


@router.delete("/{window_id}")
def delete(window_id: int, request: Request, username: str = Depends(require_operator)):
    with request.app.state.cm_session_factory() as session:
        if not delete_window(session, window_id, username):
            raise HTTPException(status_code=404, detail="Unknown maintenance window.")
    return {"deleted": True}
