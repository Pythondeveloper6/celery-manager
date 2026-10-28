"""Durable last-seen worker status from Celery events."""
from datetime import datetime, timezone

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from celery_manager.db.models import WorkerState


def record_worker_event(session, event):
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
    if previous > stamp:
        return
    row.last_seen_at = stamp
    row.state = "offline" if event["type"] == "worker-offline" else "online"
    row.active = event.get("active") if isinstance(event.get("active"), int) else None
    row.processed = event.get("processed") if isinstance(event.get("processed"), int) else None
