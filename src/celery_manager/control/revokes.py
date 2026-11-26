"""Durable revokes: workers keep revoked ids in memory only, so a revoke is
stored here, re-sent to each worker that comes online, and checked at run
start until it expires."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from celery_manager.db.models import TaskRun
from celery_manager.db.run_control import RevokedTask

ETA_MARGIN = timedelta(hours=1)
CHUNK = 100


def remember_revokes(session, task_ids, *, terminate=False, username=None, retention_hours=48, now=None) -> None:
    """Store revokes for `task_ids` (the caller commits). A run's ETA, when
    known and later, extends its expiry to the ETA plus a margin."""
    ids = list(dict.fromkeys(t for t in task_ids if t))
    if not ids:
        return
    now = now or datetime.now(timezone.utc)
    default = now + timedelta(hours=retention_hours)
    etas = dict(session.query(TaskRun.task_id, TaskRun.eta).filter(TaskRun.task_id.in_(ids)).all())
    for task_id in ids:
        eta = etas.get(task_id)
        if eta is not None and eta.tzinfo is None:
            eta = eta.replace(tzinfo=timezone.utc)
        expires = max(default, eta + ETA_MARGIN) if eta is not None else default
        row = session.get(RevokedTask, task_id) or RevokedTask(task_id=task_id)
        row.terminate, row.requested_by, row.requested_at, row.expires_at = bool(terminate), username, now, expires
        session.add(row)


def live_revokes(session, now=None) -> list[RevokedTask]:
    now = now or datetime.now(timezone.utc)
    return session.query(RevokedTask).filter(RevokedTask.expires_at > now).all()


def resend_revokes(celery_app, session, hostname: str, now=None) -> int:
    """Re-send live revokes to one worker that just came online."""
    rows = live_revokes(session, now)
    for terminate in (False, True):
        ids = [r.task_id for r in rows if bool(r.terminate) is terminate]
        for start in range(0, len(ids), CHUNK):
            celery_app.control.revoke(ids[start:start + CHUNK], terminate=terminate, destination=[hostname])
    return len(rows)


def is_revoked(session, task_id: str, now=None) -> bool:
    row = session.get(RevokedTask, task_id)
    if row is None:
        return False
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    return expires > (now or datetime.now(timezone.utc))


def prune_revokes(session, now=None, dry_run=False) -> int:
    expired = RevokedTask.expires_at <= (now or datetime.now(timezone.utc))
    query = session.query(RevokedTask).filter(expired)
    if dry_run:
        return query.count()
    deleted = query.delete(synchronize_session=False)
    session.commit()
    return deleted
