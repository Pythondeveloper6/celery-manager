from __future__ import annotations

import copy
from datetime import datetime, timezone

from celery_manager.db.models import PeriodicSchedule, ScheduleOverride


def upcoming_occurrences(schedule: PeriodicSchedule, celery_schedule, count: int = 10, from_time: datetime | None = None) -> list[datetime]:
    """Compute the next `count` run times for a periodic schedule.

    `celery_schedule` is the actual celery.schedules.schedule/crontab
    instance the host app configured for this entry (not derivable from
    our stored display string alone — callers look it up from the host
    app's beat_schedule by `schedule.name`).

    `celery.schedules.schedule.remaining_estimate()` measures "time
    remaining until due" relative to the schedule's own `now()` (real
    wall-clock time by default), not relative to the `last_run_at`
    argument — so naively adding its result to `last_run_at` mixes an
    arbitrary `from_time` cursor with the actual current time and drifts
    with however much wall-clock time has passed since the call. To get
    a deterministic sequence of occurrences starting from `from_time`,
    we pin `nowfun` to the loop cursor for each estimate — a supported
    BaseSchedule extension point — so "now" and "last_run_at" agree and
    the estimate resolves to the next occurrence after the cursor.

    `celery_schedule` objects come from the host app's `beat_schedule`
    config and are long-lived and shared across requests/threads (FastAPI
    sync routes run in a threadpool — see Task 9). Mutating `nowfun` in
    place on the shared object would race: a second thread computing
    occurrences for the same schedule could overwrite `nowfun` between
    this thread's set-and-call. To avoid that entirely, we operate on a
    private shallow copy for the duration of this call, so no shared
    state is ever mutated and there is nothing to restore.
    """
    from_time = from_time or datetime.now(timezone.utc)
    occurrences: list[datetime] = []
    last_run_at = from_time
    local_schedule = copy.copy(celery_schedule)
    for _ in range(count):
        local_schedule.nowfun = lambda cursor=last_run_at: cursor
        remaining = local_schedule.remaining_estimate(last_run_at)
        next_run = last_run_at + remaining
        occurrences.append(next_run)
        last_run_at = next_run
    return occurrences


def pause_occurrence(session, schedule_name: str, target_time: datetime, action: str, created_by: str) -> ScheduleOverride:
    """Record a skip/cancel/reschedule override for one future occurrence.
    Idempotent: re-calling for the same (schedule_name, target_time) that
    already has an unconsumed override returns the existing row."""
    existing = (
        session.query(ScheduleOverride)
        .filter_by(schedule_name=schedule_name, target_time=target_time, consumed=False)
        .one_or_none()
    )
    if existing is not None:
        existing.action = action
        session.commit()
        return existing

    override = ScheduleOverride(
        schedule_name=schedule_name,
        target_time=target_time,
        action=action,
        created_by=created_by,
        created_at=datetime.now(timezone.utc),
    )
    session.add(override)
    session.commit()
    return override


def list_overrides(session, schedule_name: str) -> list[ScheduleOverride]:
    return (
        session.query(ScheduleOverride)
        .filter_by(schedule_name=schedule_name)
        .order_by(ScheduleOverride.target_time)
        .all()
    )


def sync_periodic_schedules(session, celery_app) -> list[PeriodicSchedule]:
    """Upsert periodic_schedules from the host app's beat_schedule config.
    Call once at REST API startup (see Task 10) so the dashboard's list of
    schedules always reflects what's actually configured to run."""
    synced = []
    for name, entry in celery_app.conf.beat_schedule.items():
        row = session.query(PeriodicSchedule).filter_by(name=name).one_or_none()
        if row is None:
            row = PeriodicSchedule(name=name)
            session.add(row)
        row.task_name = entry["task"]
        row.schedule_spec = str(entry["schedule"])
        row.enabled = True
        synced.append(row)
    session.commit()
    return synced


def _as_aware_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on DateTime(timezone=True) round-trip through a
    fresh session (confirmed in Task 3's decorator work) — every write in
    this codebase uses datetime.now(timezone.utc) / tz-aware values, so a
    naive read-back is always UTC. Normalize before using a DB-sourced
    datetime as a dict key or in a Python-level comparison; SQL-level
    filter_by comparisons (e.g. in pause_occurrence above) don't need this,
    only post-fetch Python equality/hashing does."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def list_upcoming_with_overrides(session, celery_app, schedule_name: str, count: int = 10, from_time: datetime | None = None) -> list[dict]:
    """The data the dashboard's future-schedule view renders: every
    upcoming occurrence, each annotated with any existing override."""
    periodic = session.query(PeriodicSchedule).filter_by(name=schedule_name).one_or_none()
    entry = celery_app.conf.beat_schedule.get(schedule_name)
    if periodic is None or entry is None:
        return []

    occurrences = upcoming_occurrences(periodic, entry["schedule"], count=count, from_time=from_time)
    overrides_by_time = {_as_aware_utc(o.target_time): o for o in list_overrides(session, schedule_name)}

    return [
        {
            "target_time": t,
            "action": overrides_by_time[t].action if t in overrides_by_time else None,
            "consumed": overrides_by_time[t].consumed if t in overrides_by_time else False,
        }
        for t in occurrences
    ]
