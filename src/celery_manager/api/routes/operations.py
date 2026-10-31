"""Audited deployment/maintenance annotations and read-only local preflight."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from celery_manager.api.deps import get_current_user, require_admin
from celery_manager.api.serialization import utc, utc_bound
from celery_manager.control.audit import record
from celery_manager.db.identity import exact_identity, portable_text_fits
from celery_manager.db.models import OperationAnnotation

router = APIRouter(prefix="/api/operations")


class AnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["deployment", "maintenance"]
    label: str = Field(min_length=1, max_length=255)
    revision: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None


def serialize(row):
    return {"id": row.id, "kind": row.kind, "environment": row.environment, "label": row.label,
            "revision": row.revision, "note": row.note, "starts_at": utc(row.starts_at),
            "ends_at": utc(row.ends_at), "created_at": utc(row.created_at), "actor": row.actor}


@router.get("/annotations")
def annotations(request: Request, date_from: datetime | None = None, date_to: datetime | None = None,
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                username: str = Depends(get_current_user)):
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        query = session.query(OperationAnnotation).filter(exact_identity(OperationAnnotation.environment, config.environment_name))
        if date_from is not None:
            query = query.filter(OperationAnnotation.starts_at >= utc_bound(date_from))
        if date_to is not None:
            query = query.filter(OperationAnnotation.starts_at <= utc_bound(date_to))
        rows = query.order_by(OperationAnnotation.starts_at.desc(), OperationAnnotation.id.desc()).offset(offset).limit(limit + 1).all()
        return {"items": [serialize(row) for row in rows[:limit]], "has_more": len(rows) > limit,
                "environment": config.environment_name}


@router.get("/context")
def operation_context(request: Request, at: datetime, username: str = Depends(get_current_user)):
    """Operational context at a run/incident instant, never a suppression rule."""
    instant = utc_bound(at)
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        base = session.query(OperationAnnotation).filter(exact_identity(OperationAnnotation.environment, config.environment_name))
        deployment = base.filter(OperationAnnotation.kind == "deployment", OperationAnnotation.starts_at <= instant).order_by(
            OperationAnnotation.starts_at.desc(), OperationAnnotation.id.desc()).first()
        maintenance = base.filter(OperationAnnotation.kind == "maintenance", OperationAnnotation.starts_at <= instant,
                                  OperationAnnotation.ends_at > instant).order_by(OperationAnnotation.starts_at.desc()).limit(201).all()
        return {"at": utc(instant), "environment": config.environment_name,
                "deployment": serialize(deployment) if deployment else None,
                "maintenance": [serialize(row) for row in maintenance[:200]], "has_more": len(maintenance) > 200}


@router.post("/annotations")
def add_annotation(payload: AnnotationRequest, request: Request, username: str = Depends(require_admin)):
    now = datetime.now(timezone.utc)
    starts, ends = utc(utc_bound(payload.starts_at)) or now, utc(utc_bound(payload.ends_at))
    if payload.kind == "maintenance" and (ends is None or ends <= starts):
        raise HTTPException(422, "Maintenance requires an end after its start")
    if ends is not None and ends <= starts:
        raise HTTPException(422, "End must be after start")
    if (not payload.label.strip() or not payload.label.isprintable() or not portable_text_fits(payload.label, 255)
            or (payload.revision is not None and (not payload.revision.isprintable() or not portable_text_fits(payload.revision, 255)))):
        raise HTTPException(422, "Labels and revisions must be printable text of at most 255 UTF-16 code units")
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        row = OperationAnnotation(kind=payload.kind, environment=config.environment_name, label=payload.label,
                                  revision=payload.revision, note=payload.note, starts_at=starts, ends_at=ends,
                                  created_at=now, actor=str(username), actor_subject=getattr(username, "subject", None))
        session.add(row)
        session.flush()
        result = jsonable_encoder(serialize(row))
        record(session, username, "operation.annotate", "operation", str(row.id), result)
        return result


@router.get("/preflight")
def preflight(request: Request, username: str = Depends(require_admin)):
    from celery_manager.doctor import doctor
    return doctor(request.app.state.cm_config)
