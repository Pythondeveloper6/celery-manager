from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from celery.schedules import crontab, maybe_schedule, solar
from celery.schedules import schedule as interval_schedule

from celery_manager.control.audit import record as record_audit
from celery_manager.db.models import PeriodicSchedule, ScheduleOverride, TaskRun


def _entry_schedule(entry: dict):
    """The `celery.schedules` object for one `beat_schedule` entry.

    Idiomatic Celery config lets `schedule` be a bare number of seconds or a
    `timedelta`, not just a `schedule()`/`crontab()` instance — Celery's own
    beat normalizes it internally via `maybe_schedule()`, so users reasonably
    write `"schedule": 30.0` or `"schedule": timedelta(hours=2)`. We read
    these entries directly, so without the same normalization anything that
    isn't already a schedule object blew up with
    `AttributeError: 'float' object has no attribute 'nowfun'` — surfacing as
    a 500 from `GET /api/schedules/{name}/occurrences`. Normalizing here, in
    the one place both readers go through, keeps them in step.

    `app` is deliberately not passed through to `maybe_schedule`: it would
    mutate `.app` on the host's long-lived, shared schedule objects, which
    `upcoming_occurrences` goes out of its way to avoid doing.
    """
    return maybe_schedule(entry["schedule"])


def _is_interval_schedule(celery_schedule) -> bool:
    """True for a fixed-interval `celery.schedules.schedule(run_every=...)`
    (which is also what a bare number / `timedelta` entry normalizes to),
    False for wall-clock-anchored schedules like `crontab` and `solar`.
    crontab/solar are not subclasses of `schedule` today; they are excluded
    explicitly anyway so a future Celery reshuffle can't silently route them
    through interval projection."""
    return isinstance(celery_schedule, interval_schedule) and not isinstance(celery_schedule, (crontab, solar))


def _last_observed_run_at(session, schedule_name: str) -> datetime | None:
    """When this schedule most recently actually ran, as far as
    celery-manager has seen — the anchor for interval-schedule projection.

    Celery beat fires an interval entry at `last_run_at + run_every`, where
    `last_run_at` is the moment beat *sent* the previous run (beat.py
    `ScheduleEntry._next_instance` stamps `default_now()`). That state lives
    in beat's own process/shelve file and is invisible to the API process,
    but every run of a `@track`ed task writes a `task_runs` row carrying
    `schedule_name` and `started_at` (including skipped runs), which is the
    same instant plus queue latency — normally well inside
    `eta_tolerance_seconds`.

    Runs dispatched by the dashboard's retry (`retried_from_id` set) are not
    beat fires and are excluded. Ad-hoc `.delay()` calls of the same task
    cannot be told apart from beat fires (the decorator records the same
    `schedule_name` for both) — a known limitation, see
    `upcoming_occurrences`.
    """
    row = (
        session.query(TaskRun.started_at)
        .filter(
            TaskRun.schedule_name == schedule_name,
            TaskRun.started_at.isnot(None),
            TaskRun.retried_from_id.is_(None),
        )
        .order_by(TaskRun.started_at.desc())
        .first()
    )
    return _as_aware_utc(row[0]) if row is not None else None


def upcoming_occurrences(
    schedule: PeriodicSchedule,
    celery_schedule,
    count: int = 10,
    from_time: datetime | None = None,
    anchor: datetime | None = None,
) -> list[datetime]:
    """Compute the next `count` run times for a periodic schedule.

    `celery_schedule` is the actual celery.schedules.schedule/crontab
    instance the host app configured for this entry (not derivable from
    our stored display string alone — callers look it up from the host
    app's beat_schedule by `schedule.name`).

    Interval schedules and `anchor`: a `schedule(run_every=...)` has no
    wall-clock grid of its own — beat fires it at "previous fire +
    run_every". Computing it from `from_time` (i.e. "now") therefore yields
    `now + run_every`, a value that moves forward on every call and has no
    relation to when beat will actually fire. That broke the headline
    feature twice over: a saved skip/cancel stopped matching the displayed
    occurrence once more than `eta_tolerance_seconds` had passed since it
    was saved (display), and the saved `target_time` itself could be up to
    a whole interval away from the real fire time, so the decorator's
    run-time check missed it (enforcement). When `anchor` — the schedule's
    last observed actual run, see `_last_observed_run_at` — is given for an
    interval schedule, occurrences are instead projected on the fixed grid
    `anchor + k * run_every`, which is stable across calls and lines up
    with beat. Without an anchor (no run observed yet) we fall back to the
    from-`from_time` estimate below, which is only an estimate and still
    drifts; the first observed run fixes it. crontab/solar are wall-clock
    anchored already and ignore `anchor`.

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

    if anchor is not None and _is_interval_schedule(celery_schedule):
        return _project_interval(_as_aware_utc(anchor), celery_schedule.run_every, count, from_time)

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


def _project_interval(anchor: datetime, run_every: timedelta, count: int, from_time: datetime) -> list[datetime]:
    """The first `count` points of the grid `anchor + k * run_every` that
    lie strictly after `from_time`. Pure arithmetic on the two datetimes —
    deterministic for a given anchor, however much wall-clock time passes
    between calls."""
    if run_every <= timedelta(0):
        return []
    k = (from_time - anchor) // run_every + 1
    first = anchor + k * run_every
    return [first + i * run_every for i in range(count)]


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
    """Record a skip or cancel override for one future occurrence.

    `action` is validated by the caller, not here. The REST layer accepts
    only `skip` and `cancel` and rejects `reschedule` with a 422: the
    `ScheduleOverride.reschedule_to` column exists in the data model but no
    reschedule behaviour is implemented, and silently storing an override
    nothing acts on is worse than refusing it.

    Idempotent by *tolerance window*, not exact equality: occurrence
    timestamps are not guaranteed byte-identical between calls (an
    interval schedule with no observed run yet is estimated relative to
    "now", and an anchored one shifts by the queue latency each time a new
    run re-anchors it — see `upcoming_occurrences`), so two separate pause requests
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
        _audit_pause(session, created_by, schedule_name, target_time, action, updated=True)
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
    _audit_pause(session, created_by, schedule_name, target_time, action, updated=False)
    return override


def _audit_pause(session, username: str, schedule_name: str, target_time: datetime, action: str, updated: bool) -> None:
    """Record a pause_occurrence in the audit log.

    Pausing or cancelling a future occurrence is the product's headline
    operator action (spec §2) and was the one control operation with no audit
    trail at all — retry was the only audited action. Targets the schedule by
    name (the stable identifier a user acts on); the specific occurrence and
    the chosen action go in `details`, since a future occurrence has no id of
    its own until it runs.
    """
    record_audit(
        session,
        username,
        "pause_occurrence",
        "schedule",
        schedule_name,
        {"target_time": target_time.isoformat(), "action": action, "updated_existing": updated},
    )


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
        row.schedule_spec = str(_entry_schedule(entry))
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

    Interval schedules are projected from the schedule's last observed
    run (`_last_observed_run_at`), so their occurrence times are stable
    across requests once the schedule has run at least once; before that
    they are estimated relative to "now" and do drift (see
    `upcoming_occurrences`). Even anchored, a `target_time` a user paused
    via `pause_occurrence` need not be byte-identical to the same logical
    occurrence recomputed later (a new run re-anchors the grid by its queue
    latency; the client may round-trip the timestamp). Matching by `eta_tolerance_seconds` (same concept, and same
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

    celery_schedule = _entry_schedule(entry)
    anchor = _last_observed_run_at(session, schedule_name) if _is_interval_schedule(celery_schedule) else None
    occurrences = upcoming_occurrences(periodic, celery_schedule, count=count, from_time=from_time, anchor=anchor)
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
