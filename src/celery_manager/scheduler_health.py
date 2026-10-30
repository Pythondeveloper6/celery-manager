"""Advisory liveness for scheduler registries that are independent of Celery Beat."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update

from celery_manager.control.health import aware
from celery_manager.db.health_data import ExpectedSchedulerCommand, SchedulerInstance
from celery_manager.health_probes import HealthScopeUnavailable
from celery_manager.occurrences import next_occurrence_after
from celery_manager.schedule_specs import validate_spec


@dataclass(frozen=True)
class ExpectedCommand:
    name: str
    cron: str
    timezone: str = "UTC"
    grace_seconds: float = 60


def _identifier(value):
    # A canonical ASCII alphabet keeps primary-key identity stable on default
    # MySQL/MSSQL case/accent-insensitive collations and VARCHAR encodings.
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", value):
        raise ValueError("Scheduler identifiers must contain 1..128 lowercase ASCII letters, digits, _, ., :, or - and start with a letter or digit")
    return value


def register_scheduler(session, instance_id, commands, *, at=None):
    """Persist expected commands once at deployment; unchanged definitions retain anchors."""
    _identifier(instance_id)
    if not isinstance(commands, (tuple, list)) or len(commands) > 128:
        raise ValueError("Register at most 128 expected commands per scheduler")
    normalized = {}
    for item in commands:
        if not isinstance(item, ExpectedCommand):
            raise TypeError("Expected an ExpectedCommand")
        _identifier(item.name)
        if item.name in normalized:
            raise ValueError("Duplicate expected command name")
        fields = item.cron.split() if isinstance(item.cron, str) else []
        if len(fields) != 5:
            raise ValueError("Expected a five-field cron expression")
        if isinstance(item.grace_seconds, bool) or not isinstance(item.grace_seconds, (int, float)) or not math.isfinite(item.grace_seconds) or not 0 <= item.grace_seconds <= 604800:
            raise ValueError("Command grace_seconds must be between 0 and 604800")
        spec, _ = validate_spec(dict(zip(("minute", "hour", "day_of_month", "month_of_year", "day_of_week"), fields, strict=True),
                                     type="crontab", timezone=item.timezone))
        normalized[item.name] = (json.dumps(spec, sort_keys=True), item.grace_seconds)
    at = aware(at or datetime.now(timezone.utc))
    _lock_instance(session, instance_id, at)
    existing = {row.name: row for row in session.query(ExpectedSchedulerCommand).filter_by(instance_id=instance_id)}
    for name, (spec, grace) in normalized.items():
        command = existing.get(name)
        if command is None:
            command = ExpectedSchedulerCommand(instance_id=instance_id, name=name, registered_at=at)
            session.add(command)
        elif command.schedule_spec != spec:
            command.registered_at, command.observed_at = at, None
        command.schedule_spec, command.grace_seconds = spec, grace
    for name in set(existing) - set(normalized):
        session.delete(existing[name])
    session.commit()



def _lock_instance(session, instance_id, at):
    from sqlalchemy import insert, select
    from sqlalchemy.exc import IntegrityError
    query = select(SchedulerInstance).where(SchedulerInstance.instance_id == instance_id).with_for_update().with_hint(SchedulerInstance, "WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql").execution_options(populate_existing=True)
    dialect = session.get_bind().dialect.name
    values = {"instance_id": instance_id, "registered_at": at}
    if dialect in ("sqlite", "postgresql"):
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        session.execute(dialect_insert(SchedulerInstance).values(**values).on_conflict_do_nothing(index_elements=["instance_id"]))
    elif dialect in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import insert as dialect_insert
        statement = dialect_insert(SchedulerInstance).values(**values)
        session.execute(statement.on_duplicate_key_update(instance_id=statement.inserted.instance_id))
    elif session.scalar(query) is None:
        try:
            with session.begin_nested():
                session.execute(insert(SchedulerInstance).values(**values))
        except IntegrityError:
            pass
    return session.scalar(query)

def scheduler_heartbeat(session, instance_id, *, at=None):
    row = session.get(SchedulerInstance, _identifier(instance_id))
    if row is None:
        raise KeyError("Register the scheduler instance before reporting heartbeats")
    at = aware(at or datetime.now(timezone.utc))
    session.execute(update(SchedulerInstance).where(
        SchedulerInstance.instance_id == instance_id, SchedulerInstance.registered_at <= at,
        or_(SchedulerInstance.heartbeat_at.is_(None), SchedulerInstance.heartbeat_at < at)
    ).values(heartbeat_at=at).execution_options(synchronize_session="fetch"))
    session.commit()


def scheduler_command_observed(session, instance_id, name, *, at=None):
    row = session.get(ExpectedSchedulerCommand, (_identifier(instance_id), _identifier(name)))
    if row is None:
        raise KeyError("Unknown expected scheduler command")
    at = aware(at or datetime.now(timezone.utc))
    session.execute(update(ExpectedSchedulerCommand).where(
        ExpectedSchedulerCommand.instance_id == instance_id, ExpectedSchedulerCommand.name == name,
        ExpectedSchedulerCommand.registered_at <= at,
        or_(ExpectedSchedulerCommand.observed_at.is_(None), ExpectedSchedulerCommand.observed_at < at)
    ).values(observed_at=at).execution_options(synchronize_session="fetch"))
    session.commit()


def retire_scheduler(session, instance_id):
    """Remove monitoring expectations only; no host scheduler is ever contacted."""
    _identifier(instance_id)
    # Serialize retirement with registry replacement on the same parent row.
    row = session.scalar(select(SchedulerInstance).where(SchedulerInstance.instance_id == instance_id)
                         .with_for_update().with_hint(SchedulerInstance, "WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql"))
    if row is None:
        session.commit()
        return False
    session.execute(delete(ExpectedSchedulerCommand).where(ExpectedSchedulerCommand.instance_id == instance_id))
    session.delete(row)
    session.commit()
    return True


def scheduler_status(session, *, stale_seconds=120, now=None):
    now = aware(now or datetime.now(timezone.utc))
    result = []
    instances = session.query(SchedulerInstance).order_by(SchedulerInstance.instance_id).limit(1001).all()
    if len(instances) > 1000:
        raise HealthScopeUnavailable("Scheduler scope exceeds 1000; retire obsolete registrations")
    grouped = {item.instance_id: [] for item in instances}
    all_commands = session.query(ExpectedSchedulerCommand).filter(ExpectedSchedulerCommand.instance_id.in_(list(grouped))).order_by(ExpectedSchedulerCommand.instance_id, ExpectedSchedulerCommand.name).limit(20001).all() if grouped else []
    truncated = len(all_commands) > 20000
    for command in all_commands[:20000]:
        grouped[command.instance_id].append(command)
    for instance in instances:
        heartbeat = aware(instance.heartbeat_at)
        age = max(0, (now - (heartbeat or aware(instance.registered_at))).total_seconds())
        state = "degraded" if age > stale_seconds else "healthy" if heartbeat else "unknown"
        commands = []
        for command in grouped[instance.instance_id] if not truncated else []:
            try:
                spec, timing = validate_spec(json.loads(command.schedule_spec))
                anchor = aware(command.observed_at or command.registered_at)
                due = next_occurrence_after(timing, anchor)
                overdue = now > due + timedelta(seconds=command.grace_seconds)
                command_state = "degraded" if overdue else "healthy"
            except (ValueError, TypeError, OverflowError, KeyError):
                spec, due, command_state = {}, None, "unknown"
            commands.append({"name": command.name, "state": command_state, "schedule": spec,
                             "due_at": due.isoformat() if due else None, "observed_at": aware(command.observed_at).isoformat() if command.observed_at else None,
                             "grace_seconds": command.grace_seconds, "first_run": command.observed_at is None})
        if truncated:
            state = "unknown"
        elif any(item["state"] == "degraded" for item in commands):
            state = "degraded"
        elif state != "degraded" and any(item["state"] == "unknown" for item in commands):
            state = "unknown"
        result.append({"instance_id": instance.instance_id, "state": state,
                       "heartbeat_at": heartbeat.isoformat() if heartbeat else None, "age_seconds": age,
                       "commands": commands})
    return result
