from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from celery_manager.api.deps import get_current_user
from celery_manager.db.models import AuditLog

router = APIRouter(prefix="/api/audit")


@router.get("")
def list_audit(request: Request, limit: int = 50, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        rows = session.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
        return [
            {"username": r.username, "action": r.action, "target_type": r.target_type, "target_id": r.target_id, "details": r.details, "created_at": r.created_at}
            for r in rows
        ]
