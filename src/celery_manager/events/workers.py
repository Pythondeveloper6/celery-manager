"""Durable last-seen worker status from Celery events."""
import time
from datetime import datetime, timedelta, timezone

_last_pruned = {}

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from celery_manager.db.models import WorkerHeartbeatSample, WorkerState


def record_worker_event(session, event, *, retention_days=7):
    if event.get("type") not in ("worker-online", "worker-heartbeat", "worker-offline"):
        return
    hostname = event.get("hostname")
    if not isinstance(hostname, str) or not hostname or len(hostname) > 255:
        return
    raw = event.get("timestamp")
    try:
        stamp = datetime.fromtimestamp(float(raw), timezone.utc) if raw is not None else datetime.now(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return
    values = {"hostname": hostname, "last_seen_at": stamp,
              "state": "offline" if event["type"] == "worker-offline" else "online"}
    query = (select(WorkerState).where(WorkerState.hostname == hostname)
             .with_hint(WorkerState, "WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql")
             .with_for_update().execution_options(populate_existing=True))
    dialect = session.get_bind().dialect.name
    # SQL Server takes an absent-key range lock before inserting. SQLite's
    # INSERT takes the write lock first. Other servers use their native
    # conflict handling, then lock the row before judging event order.
    row = session.execute(query).scalar_one_or_none() if dialect == "mssql" else None
    if row is None:
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
            session.execute(dialect_insert(WorkerState).values(**values).on_conflict_do_nothing(index_elements=["hostname"]))
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
            session.execute(dialect_insert(WorkerState).values(**values).on_conflict_do_nothing(index_elements=["hostname"]))
        elif dialect in ("mysql", "mariadb"):
            from sqlalchemy.dialects.mysql import insert as dialect_insert
            statement = dialect_insert(WorkerState).values(**values)
            session.execute(statement.on_duplicate_key_update(hostname=statement.inserted.hostname))
        else:
            try:
                with session.begin_nested():
                    session.execute(insert(WorkerState).values(**values))
            except IntegrityError:
                pass
        row = session.execute(query).scalar_one()
    previous = row.last_seen_at
    previous = previous.replace(tzinfo=timezone.utc) if previous.tzinfo is None else previous.astimezone(timezone.utc)
    if previous > stamp or (previous == stamp and row.state == "offline" and event["type"] != "worker-offline"):
        return
    row.last_seen_at = stamp
    row.state = "offline" if event["type"] == "worker-offline" else "online"
    def counter(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    row.active = counter(event.get("active"))
    row.processed = counter(event.get("processed"))

    # The WorkerState row remains locked through the caller's commit. All
    # writers for this hostname therefore serialize sample get/create too.
    if retention_days:
        now = datetime.now(timezone.utc)
        if stamp < now - timedelta(days=retention_days):
            return
        bucket = stamp.replace(second=0, microsecond=0)
        sample = session.get(WorkerHeartbeatSample, (hostname, bucket))
        if sample is None:
            sample = WorkerHeartbeatSample(hostname=hostname, bucket_at=bucket)
            session.add(sample)
        sample.observed_at, sample.state = stamp, row.state
        sample.active, sample.processed = row.active, row.processed
        key = id(session.get_bind())
        if time.monotonic() - _last_pruned.get(key, 0) >= 3600:
            # No commit here: do not release the worker row lock halfway through
            # recording. Flush/commit remains owned by the event listener.
            session.query(WorkerHeartbeatSample).filter(
                WorkerHeartbeatSample.observed_at < now-timedelta(days=retention_days)).delete(synchronize_session=False)
            _last_pruned[key] = time.monotonic()
