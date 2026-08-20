from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from celery_manager.api.deps import get_current_user
from celery_manager.api.serialization import utc
from celery_manager.control.tasks import MAX_PAGE_SIZE
from celery_manager.db.models import AuditLog

router = APIRouter(prefix="/api/audit")


@router.get("")
def list_audit(request: Request, limit: int = 50, username: str = Depends(get_current_user)):
    """The most recent audit entries, newest first.

    `limit` is clamped the same way `GET /api/tasks` clamps its own — and for
    a sharper reason here: SQLite reads `LIMIT -1` as *no limit*, so
    `?limit=-1` handed back the entire audit log in one response. Same
    reasoning as the task list: clamp rather than reject, because a stale
    bookmark deserves a capped page rather than a 400.
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        rows = session.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
        return [
            {
                "username": r.username,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "details": r.details,
                # Marked UTC for the same reason the task routes mark theirs:
                # SQLite hands the value back naive, and the audit screen
                # renders it with `new Date(...).toLocaleString()`, which reads
                # an unmarked timestamp as local time and shifts every entry by
                # the viewer's offset.
                "created_at": utc(r.created_at),
            }
            for r in rows
        ]
