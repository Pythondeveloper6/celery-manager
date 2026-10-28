"""Backend snapshots and history-preserving synchronization."""
from __future__ import annotations

import json
import threading
import time

from celery_manager.db.models import PeriodicSchedule
from celery_manager.schedule_specs import spec_from_schedule
from celery_manager.schedulers.base import StaticBeatBackend


def backend_for(config):
    return config.scheduler_backend or StaticBeatBackend(config.celery_app)


def definition(entry):
    return json.loads(json.dumps({"name": entry.name, "task": entry.task, "schedule": spec_from_schedule(entry.schedule),
            "args": entry.args, "kwargs": entry.kwargs, "options": entry.options,
            "enabled": entry.enabled, "source": entry.source}, default=str))


def _locked_schedule(session, name, values):
    from sqlalchemy import insert, select
    from sqlalchemy.exc import IntegrityError
    dialect = session.get_bind().dialect.name
    query = (select(PeriodicSchedule).where(PeriodicSchedule.name == name)
             .with_hint(PeriodicSchedule, "WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql")
             .with_for_update().execution_options(populate_existing=True))
    # SQL Server holds the absent key range before insert; SQLite takes its
    # write lock through the native insert before reading. Other backends use
    # native conflict handling for independently started API processes.
    if dialect == "mssql":
        row = session.execute(query).scalar_one_or_none()
        if row is not None:
            return row
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
        session.execute(dialect_insert(PeriodicSchedule).values(**values).on_conflict_do_nothing(index_elements=["name"]))
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
        session.execute(dialect_insert(PeriodicSchedule).values(**values).on_conflict_do_nothing(index_elements=["name"]))
    elif dialect in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import insert as dialect_insert
        statement = dialect_insert(PeriodicSchedule).values(**values)
        session.execute(statement.on_duplicate_key_update(name=statement.inserted.name))
    else:
        try:
            with session.begin_nested():
                session.execute(insert(PeriodicSchedule).values(**values))
        except IntegrityError:
            pass
    return session.execute(query).scalar_one()


def sync_from_backend(session, backend, entries=None):
    # Fetch everything before touching storage: a backend outage must not make
    # every schedule appear deleted. Keep override history on removed entries.
    entries = list(backend.list()) if entries is None else list(entries)
    names = set()
    for entry in entries:
        names.add(entry.name)
        row = _locked_schedule(session, entry.name, {
            "name": entry.name, "task_name": entry.task,
            "schedule_spec": str(entry.schedule), "enabled": entry.enabled,
        })
        row.task_name, row.schedule_spec = entry.task, str(entry.schedule)
        row.args, row.kwargs = json.dumps(entry.args, default=repr), json.dumps(entry.kwargs, default=repr)
        row.enabled, row.source = entry.enabled, backend.name
        row.definition = json.dumps(definition(entry), default=str, sort_keys=True)
    for row in session.query(PeriodicSchedule).all():
        if row.name not in names:
            row.enabled = False
    session.commit()
    return entries


class ScheduleService:
    def __init__(self, config, session_factory):
        self.config, self.sessions = config, session_factory
        self.backend = backend_for(config)
        self.entries = {}
        self.checked_at = 0
        self.lock = threading.Lock()

    def refresh(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.checked_at < self.config.schedule_sync_interval_seconds:
                return
            entries = list(self.backend.list())
            with self.sessions() as session:
                sync_from_backend(session, self.backend, entries)
            self.entries = {entry.name: entry.as_beat_entry() for entry in entries}
            self.checked_at = time.monotonic()


def entries_for(session, celery_app):
    config = session.info.get("cm_config")
    service = getattr(config, "_schedule_service", None)
    if config is not None and config.scheduler_backend is not None:
        if service is not None:
            return service.entries
        return {entry.name: entry.as_beat_entry() for entry in config.scheduler_backend.list()}
    return celery_app.conf.beat_schedule or {}
