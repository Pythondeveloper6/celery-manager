"""Backend-owned schedule CRUD, preview and common operator actions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from celery_manager.api.deps import (
    Principal,
    require_admin,
    require_operator,
    require_role,
)
from celery_manager.api.routes.schedules import refresh_backend
from celery_manager.api.serialization import utc
from celery_manager.control.audit import record
from celery_manager.control.schedules import (
    list_upcoming_with_overrides,
    pause_occurrence,
    upcoming_occurrences,
)
from celery_manager.control.tasks import start_task
from celery_manager.cron import RuleBasedParser
from celery_manager.db.models import PeriodicSchedule
from celery_manager.schedule_specs import ScheduleSpecError, validate_spec
from celery_manager.schedulers.base import ScheduleEntry
from celery_manager.schedulers.service import definition

require_viewer = require_role("viewer")

router = APIRouter(prefix="/api/schedules", dependencies=[Depends(refresh_backend)])


class CreateSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255, pattern=r"^[^/\x00-\x1f]+$")
    task: str
    schedule: dict
    args: list = Field(default_factory=list)
    kwargs: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)
    enabled: bool = True


class UpdateSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule: dict | None = None
    args: list | None = None
    kwargs: dict | None = None
    options: dict | None = None
    enabled: bool | None = None


def parse_schedule(data, config):
    if data.get("type") == "text":
        parser = config.cron_parser or RuleBasedParser()
        data = parser.parse(data.get("text", ""), str(config.celery_app.timezone))
    return validate_spec(data, app=config.celery_app)


def writable(config):
    backend = config._schedule_service.backend
    if not backend.writable:
        raise HTTPException(409, "This scheduler backend is read-only.")
    return backend


def validate_options(options):
    allowed = {"queue", "exchange", "routing_key", "priority", "expires", "headers"}
    if set(options) - allowed:
        raise HTTPException(422, "Unsupported delivery option")
    headers = options.get("headers") or {}
    if not isinstance(headers, dict) or any(str(k).startswith("cm_") for k in headers):
        raise HTTPException(422, "Reserved celery-manager headers cannot be supplied")


def change(config, sessions, username, action, before, after, name):
    with sessions() as session:
        record(session, username, f"schedule.{action}", "schedule", name,
               {"before": definition(before) if before else None, "after": definition(after) if after else None})
    config._schedule_service.refresh(force=True)


@router.post("/preview")
def preview_new(payload: dict, request: Request, username: str = Depends(require_viewer)):
    config = request.app.state.cm_config
    try:
        spec, schedule = parse_schedule(payload.get("schedule", payload), config)
        row = PeriodicSchedule(name="preview", task_name="preview", schedule_spec=str(schedule))
        times = upcoming_occurrences(row, schedule, count=10)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"schedule": spec, "description": str(schedule), "occurrences": [utc(t) for t in times],
            "timezone": str(config.celery_app.timezone)}


@router.post("")
def create(payload: CreateSchedule, request: Request, username: str = Depends(require_admin)):
    config = request.app.state.cm_config
    backend = writable(config)
    config.celery_app.loader.import_default_modules()
    if payload.task not in config.celery_app.tasks or payload.task.startswith("celery."):
        raise HTTPException(422, "Task is not registered on this Celery app")
    validate_options(payload.options)
    try:
        _, schedule = parse_schedule(payload.schedule, config)
        entry = ScheduleEntry(payload.name, payload.task, schedule, payload.args, payload.kwargs,
                              payload.options, payload.enabled, source=backend.name)
        saved = backend.create(entry)
    except FileExistsError as exc:
        raise HTTPException(409, "A schedule with this name already exists") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    change(config, request.app.state.cm_session_factory, username, "create", None, saved, saved.name)
    return definition(saved)


@router.patch("/{name}")
def update(name: str, payload: UpdateSchedule, request: Request, username: str = Depends(require_operator)):
    config = request.app.state.cm_config
    backend = writable(config)
    before = backend.get(name)
    if before is None:
        raise HTTPException(404, "Unknown schedule")
    changes = payload.model_dump(exclude_unset=True)
    if any(v is None for v in changes.values()):
        raise HTTPException(422, "Schedule fields cannot be null")
    if "options" in changes:
        validate_options(changes["options"])
    try:
        if "schedule" in changes:
            _, changes["schedule"] = parse_schedule(changes["schedule"], config)
        saved = backend.update(name, **changes)
    except (ValueError, TypeError, ScheduleSpecError) as exc:
        raise HTTPException(422, str(exc)) from exc
    change(config, request.app.state.cm_session_factory, username, "update", before, saved, name)
    return definition(saved)


@router.delete("/{name}")
def delete(name: str, request: Request, username: str = Depends(require_admin)):
    config = request.app.state.cm_config
    backend = writable(config)
    before = backend.get(name)
    if before is None:
        raise HTTPException(404, "Unknown schedule")
    backend.delete(name)
    change(config, request.app.state.cm_session_factory, username, "delete", before, None, name)
    return {"ok": True}


@router.post("/{name}/skip-next")
def skip_next(name: str, request: Request, username: str = Depends(require_operator)):
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        times = list_upcoming_with_overrides(session, config.celery_app, name, count=1,
                                             apply_edits=config.schedule_editing)
        entry = config._schedule_service.entries.get(name)
        if not times or not entry or not entry.get("enabled", True):
            raise HTTPException(409, "No enabled upcoming occurrence is available")
        target = times[0]["target_time"]
        pause_occurrence(session, name, target, "skip", username,
                         eta_tolerance_seconds=config.eta_tolerance_seconds)
    return {"target_time": utc(target)}


@router.post("/{name}/run-now")
def run_now(name: str, request: Request, principal: Principal, username: str = Depends(require_operator)):
    config = request.app.state.cm_config
    entry = config._schedule_service.backend.get(name)
    if entry is None:
        raise HTTPException(404, "Unknown schedule")
    # No occurrence headers: a manual run must not consume skip-next.
    with request.app.state.cm_session_factory() as session:
        session.info["cm_principal"] = principal
        task_id = start_task(config.celery_app, entry.task, entry.args, entry.kwargs,
                             session=session, username=username, queue=entry.options.get("queue"),
                             _preserve_queue=True,
                             _delivery_options={k: v for k, v in entry.options.items() if k != "queue"})
        record(session, username, "schedule.run_now", "schedule", name, {"task_id": task_id})
    return {"task_id": task_id}


@router.get("/health")
def health(request: Request, username: str = Depends(require_viewer)):
    config = request.app.state.cm_config
    config.celery_app.loader.import_default_modules()
    items = []
    for name, entry in config._schedule_service.entries.items():
        if entry["task"] not in config.celery_app.tasks:
            items.append({"name": name, "kind": "unregistered_task", "task": entry["task"]})
    return {"items": items, "backend": config._schedule_service.backend.name}


@router.get("/backend")
def backend_info(request: Request, username: str = Depends(require_viewer)):
    service = request.app.state.cm_config._schedule_service
    return {"name": service.backend.name, "writable": service.backend.writable,
            "beat_scheduler": service.backend.beat_scheduler_class()}
