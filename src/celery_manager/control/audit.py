from __future__ import annotations

import json
from datetime import datetime, timezone

from celery_manager.db.models import AuditLog


def record(session, username: str, action: str, target_type: str, target_id: str, details: dict | None = None, *, actor_subject=None) -> AuditLog:
    row = AuditLog(
        username=username,
        actor_subject=actor_subject or getattr(username, "subject", None) or getattr(session.info.get("cm_principal"), "subject", None),
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=json.dumps(details) if details is not None else None,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    return row
