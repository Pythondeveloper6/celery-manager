"""Reviewable adoption and backup of scheduler-owned definitions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from celery_manager.api.deps import require_admin, require_role
from celery_manager.api.routes.schedule_backend import validate_options
from celery_manager.control.audit import record
from celery_manager.schedulers.base import StaticBeatBackend
from celery_manager.schedulers.reconcile import (
    ReconcileConflict,
    apply_reconcile,
    export_backend,
    load_export,
    plan_reconcile,
)

router = APIRouter(prefix="/api/schedule-reconciliation")
require_viewer = require_role("viewer")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ownership: str = "backend"
    allow_updates: bool = False
    document: dict | None = None


@router.get("/export")
def export(request: Request, username: str = Depends(require_viewer)):
    try:
        return export_backend(request.app.state.cm_config._schedule_service.backend)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/plan")
def plan(payload: PlanRequest, request: Request, username: str = Depends(require_admin)):
    config = request.app.state.cm_config
    try:
        desired = (load_export(payload.document, config.celery_app) if payload.document is not None
                   else StaticBeatBackend(config.celery_app).list())
        return plan_reconcile(config._schedule_service.backend, desired, ownership=payload.ownership,
                              allow_updates=payload.allow_updates)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/apply")
def apply(payload: dict, request: Request, username: str = Depends(require_admin)):
    config = request.app.state.cm_config
    # Validate every intended task and reserved-header boundary before writes.
    config.celery_app.loader.import_default_modules()
    try:
        if not isinstance(payload.get("changes"), list):
            raise TypeError("Plan changes must be a list")
        for change in payload["changes"]:
            if isinstance(change, dict) and change.get("apply"):
                value = change.get("after")
                if not isinstance(value, dict) or not isinstance(value.get("task"), str):
                    raise TypeError("Invalid schedule definition")
                if value.get("task") not in config.celery_app.tasks or value.get("task", "").startswith("celery."):
                    raise ValueError("Task is not registered on this Celery app")
                options = value.get("options", {})
                if not isinstance(options, dict):
                    raise TypeError("Schedule options must be an object")
                validate_options(options)
        with request.app.state.cm_session_factory() as session:
            record(session, username, "schedule.reconcile_requested", "schedule", "*",
                   {"ownership": payload.get("ownership"),
                    "targets": [change.get("name") for change in payload["changes"]
                                if isinstance(change, dict) and change.get("apply")]})
        result = apply_reconcile(config._schedule_service.backend, payload, app=config.celery_app)
    except (ReconcileConflict, FileExistsError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    with request.app.state.cm_session_factory() as session:
        record(session, username, "schedule.reconcile", "schedule", "*", result)
    config._schedule_service.refresh(force=True)
    return result
