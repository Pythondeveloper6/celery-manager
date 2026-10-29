from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from celery_manager.db.models import WorkerHeartbeatSample


def prune_heartbeats(session, retention_days=7, *, now=None, dry_run=False):
    if not retention_days:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    query = session.query(WorkerHeartbeatSample).filter(WorkerHeartbeatSample.observed_at < cutoff)
    count = query.count()
    if not dry_run and count:
        session.execute(delete(WorkerHeartbeatSample).where(WorkerHeartbeatSample.observed_at < cutoff))
        session.commit()
    return count


def list_heartbeats(session, hostname, *, since=None, until=None, limit=120, offset=0):
    query = session.query(WorkerHeartbeatSample).filter_by(hostname=hostname)
    if since is not None:
        query = query.filter(WorkerHeartbeatSample.observed_at >= since)
    if until is not None:
        query = query.filter(WorkerHeartbeatSample.observed_at <= until)
    total = query.count()
    return query.order_by(WorkerHeartbeatSample.bucket_at.desc()).offset(offset).limit(limit).all(), total
