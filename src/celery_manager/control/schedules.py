from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

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


def _as_aware_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on DateTime(timezone=True) round-trip through a
    fresh session (confirmed in Task 3's decorator work) — every write in
    this codebase uses datetime.now(timezone.utc) / tz-aware values, so a
    naive read-back is always UTC. Normalize before using a DB-sourced
    datetime as a dict key or in a Python-level comparison; SQL-level
    filter_by comparisons don't need this, only post-fetch Python
    equality/hashing and arithmetic does."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _find_within_tolerance(candidates: list[ScheduleOverride], t: datetime, window: timedelta) -> ScheduleOverride | None:
    """Same matching concept as the decorator's `_find_matching_override`
    (celery_manager/decorator.py): a stored `target_time` is treated as
    "the same occurrence" as `t` if it falls within `window` of it,
    rather than requiring exact equality. Picks the closest candidate
    within the window (deterministic tie-break) rather than the first.

    This only picks a match from whatever `candidates` it's given — it
    does not itself prevent the same override from being returned as the
    closest match to two different calls (e.g. two nearby occurrences in
    `list_upcoming_with_overrides`, or a pool that already contains a
    stale duplicate). Callers that iterate multiple targets against a
    shared pool are responsible for removing a matched candidate from
    that pool before matching the next target, if a given override must
    never be claimed twice — see `list_upcoming_with_overrides` below."""
    best: ScheduleOverride | None = None
    best_diff: timedelta | None = None
    for override in candidates:
        diff = abs(_as_aware_utc(override.target_time) - t)
        if diff <= window and (best is None or diff < best_diff):
            best = override
            best_diff = diff
    return best


def pause_occurrence(
    session,
    schedule_name: str,
    target_time: datetime,
    action: str,
    created_by: str,
    eta_tolerance_seconds: int = 60,
) -> ScheduleOverride:
    """Record a skip/cancel/reschedule override for one future occurrence.

    Idempotent by *tolerance window*, not exact equality: since
    `upcoming_occurrences` recomputes occurrence timestamps relative to
    "now" on every call (see its docstring), two separate pause requests
    a human would consider "about the same occurrence" (e.g. re-deciding
    the action after a page reload) can arrive with slightly different
    `target_time` values. Re-calling with a `target_time` within
    `eta_tolerance_seconds` of an existing unconsumed override for this
    schedule updates that row's `action` *and* `target_time` (the most
    recent request wins) instead of creating a second row — this is what
    keeps `list_upcoming_with_overrides`' read-side matching
    (`_find_within_tolerance`) from ever having two overlapping,
    ambiguous rows to arbitrate between for the same logical occurrence.
    """
    window = timedelta(seconds=eta_tolerance_seconds)
    candidates = (
        session.query(ScheduleOverride)
        .filter_by(schedule_name=schedule_name, consumed=False)
        .all()
    )
    existing = _find_within_tolerance(candidates, target_time, window)
    if existing is not None:
        existing.action = action
        existing.target_time = target_time
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


def list_upcoming_with_overrides(
    session,
    celery_app,
    schedule_name: str,
    count: int = 10,
    from_time: datetime | None = None,
    eta_tolerance_seconds: int = 60,
) -> list[dict]:
    """The data the dashboard's future-schedule view renders: every
    upcoming occurrence, each annotated with any existing override.

    `upcoming_occurrences` recomputes occurrence timestamps relative to
    "now" on every call (see its docstring), so a `target_time` a user
    paused a few seconds or minutes ago via `pause_occurrence` will not
    be byte-identical to the same logical occurrence recomputed just
    now. Matching by `eta_tolerance_seconds` (same concept, and same
    default, as the decorator's own skip-check window — see
    `celery_manager.decorator._find_matching_override` — configured via
    `CeleryManagerConfig.eta_tolerance_seconds`) instead of exact
    equality is what keeps a paused occurrence showing as paused across
    that drift. This is read-only: unlike the decorator's matching, we
    never mark an override consumed here — that only happens when the
    task actually runs (or is skipped) at its scheduled time.

    Occurrences are matched against the unconsumed-override pool one at
    a time, in chronological order, and a matched override is removed
    from the pool before matching the next occurrence ("claimed"). This
    prevents the same override being reported against two adjacent
    occurrences — which would otherwise be possible for any schedule
    whose interval is shorter than `2 * eta_tolerance_seconds`, since an
    override could then legitimately fall within the tolerance window of
    two neighboring occurrences at once.
    """
    periodic = session.query(PeriodicSchedule).filter_by(name=schedule_name).one_or_none()
    entry = celery_app.conf.beat_schedule.get(schedule_name)
    if periodic is None or entry is None:
        return []

    occurrences = upcoming_occurrences(periodic, entry["schedule"], count=count, from_time=from_time)
    pool = [o for o in list_overrides(session, schedule_name) if not o.consumed]
    window = timedelta(seconds=eta_tolerance_seconds)

    result = []
    for t in occurrences:
        match = _find_within_tolerance(pool, t, window)
        if match is not None:
            pool.remove(match)
        result.append({
            "target_time": t,
            "action": match.action if match is not None else None,
            "consumed": match.consumed if match is not None else False,
        })
    return result


def list_periodic_schedules(session) -> list[PeriodicSchedule]:
    """Every configured periodic schedule, for the dashboard's schedule-list
    screen — populated by sync_periodic_schedules() at create_app startup,
    this is a pure read."""
    return session.query(PeriodicSchedule).order_by(PeriodicSchedule.name).all()
