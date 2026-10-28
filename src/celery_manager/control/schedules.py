from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from celery.schedules import maybe_schedule

from celery_manager.control.audit import record as record_audit
from celery_manager.control.maintenance import enabled_windows, windows_active_at
from celery_manager.db.models import PeriodicSchedule, ScheduleOverride, TaskRun
from celery_manager.occurrences import (
    CALENDAR_MATCH_WINDOW,
    is_interval_schedule,
    next_occurrence_after,
)
from celery_manager.schedule_specs import (
    ScheduleSpecError,
    build_schedule,
    dump_spec,
    load_spec,
    spec_from_schedule,
    validate_spec,
)

logger = logging.getLogger(__name__)


def _entry_schedule(entry: dict, override: str | None = None, app=None):
    """The `celery.schedules` object for one `beat_schedule` entry.

    With `override` (a stored `PeriodicSchedule.schedule_override`, see
    `effective_schedule`) the operator's edit wins over the code. This is
    the one place a schedule object is made for projecting or matching
    occurrences, so every reader agrees on which schedule is in force.
    A stored edit that no longer parses (hand-written, or from a newer
    version) is logged and ignored: the code's schedule is a better guess
    than a 500. `app` only reaches an edit's own new schedule object (it
    sets the crontab's timezone); see below for why the code's is left alone.

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
    if override:
        try:
            spec = load_spec(override)
        except ScheduleSpecError:
            logger.warning("celery-manager: ignoring an unreadable schedule edit: %r", override)
        else:
            if spec is not None:
                try:
                    return build_schedule(spec, app=app)
                except ScheduleSpecError:
                    logger.warning("celery-manager: ignoring an invalid schedule edit: %r", override)
    return maybe_schedule(entry["schedule"])


def effective_schedule(session, schedule_name: str, entry: dict, app=None, apply_edits: bool = True):
    """The schedule in force for `schedule_name`: the operator's saved edit
    if there is one, else the code's `beat_schedule` entry.

    Use this (not `_entry_schedule(entry)` alone) wherever occurrences are
    projected or matched. `apply_edits=False` reads the code's schedule even
    when an edit is stored: the API passes `CeleryManagerConfig.
    schedule_editing`, since an edit only takes effect when beat runs
    `celery_manager.beat:DatabaseScheduler`, and projecting an edit beat is
    not applying would show runs that never happen."""
    override = None
    if apply_edits:
        override = (
            session.query(PeriodicSchedule.schedule_override)
            .filter_by(name=schedule_name)
            .scalar()
        )
    return _entry_schedule(entry, override, app=app)


def _is_interval_schedule(celery_schedule) -> bool:
    """True for a fixed-interval `celery.schedules.schedule(run_every=...)`
    (which is also what a bare number / `timedelta` entry normalizes to),
    False for wall-clock-anchored schedules like `crontab` and `solar`.
    crontab/solar are not subclasses of `schedule` today; they are excluded
    explicitly anyway so a future Celery reshuffle can't silently route them
    through interval projection."""
    return is_interval_schedule(celery_schedule)


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

    When beat runs with the stamping scheduler (`celery_manager.beat`), runs
    carry `beat_sent_at` — beat's own `last_run_at` for that firing — and the
    newest one is used instead. It is exact: no queue latency, and manual
    calls (which carry no stamp) cannot move it.
    """
    stamped = (
        session.query(TaskRun.beat_sent_at)
        .filter(TaskRun.schedule_name == schedule_name, TaskRun.beat_sent_at.isnot(None))
        .order_by(TaskRun.beat_sent_at.desc())
        .first()
    )
    if stamped is not None:
        return _as_aware_utc(stamped[0])
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

    # `next_occurrence_after` is the same step the beat scheduler mixin uses
    # to stamp `cm_occurrence` (celery_manager.occurrences), so a calendar
    # occurrence listed here is the exact value beat will stamp for it.
    occurrences: list[datetime] = []
    cursor = from_time
    for _ in range(count):
        cursor = next_occurrence_after(celery_schedule, cursor)
        occurrences.append(cursor)
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


def get_periodic_schedule(session, schedule_name: str) -> PeriodicSchedule | None:
    return session.query(PeriodicSchedule).filter_by(name=schedule_name).one_or_none()


def is_paused(row: PeriodicSchedule, now: datetime | None = None) -> bool:
    """The effective pause state: `paused_at` is set AND the pause has no end
    (`paused_until` NULL) or the end is still in the future."""
    if row.paused_at is None:
        return False
    if row.paused_until is None:
        return True
    return (now or datetime.now(timezone.utc)) < _as_aware_utc(row.paused_until)


def expire_stale_pause(session, schedule_name: str, paused_until, now: datetime | None = None) -> bool:
    """Clear a pause whose `paused_until` has passed and audit it — exactly
    once, however many readers notice at the same moment.

    `paused_until` is the stored value the caller read. The UPDATE is
    conditional on it still being there, so of N concurrent callers only the
    one whose UPDATE matches a row (rowcount 1) writes the automatic
    `resume_schedule` audit row; the rest see 0 and do nothing. Returns True
    if this call did the clearing.
    """
    before = (
        session.query(PeriodicSchedule.paused_at, PeriodicSchedule.paused_by)
        .filter_by(name=schedule_name, paused_until=paused_until)
        .one_or_none()
    )
    if before is None or before.paused_at is None:
        return False
    cleared = (
        session.query(PeriodicSchedule)
        .filter(PeriodicSchedule.name == schedule_name, PeriodicSchedule.paused_until == paused_until)
        .update(
            {PeriodicSchedule.paused_at: None, PeriodicSchedule.paused_by: None, PeriodicSchedule.paused_until: None},
            synchronize_session=False,
        )
    )
    session.commit()
    if cleared != 1:
        return False
    record_audit(
        session,
        "system",
        "resume_schedule",
        "schedule",
        schedule_name,
        {
            "automatic": True,
            "paused_at": _as_aware_utc(before.paused_at).isoformat(),
            "paused_by": before.paused_by,
            "paused_until": _as_aware_utc(paused_until).isoformat(),
        },
    )
    return True


def _expire_if_stale(session, row: PeriodicSchedule, now: datetime | None = None) -> None:
    """Lazily apply an elapsed `paused_until` to `row` (see `expire_stale_pause`)."""
    if row.paused_at is None or row.paused_until is None:
        return
    now = now or datetime.now(timezone.utc)
    if not is_paused(row, now):
        expire_stale_pause(session, row.name, row.paused_until, now)
        session.refresh(row)


def pause_schedule(
    session, schedule_name: str, username: str, until: datetime | None = None
) -> PeriodicSchedule | None:
    """Pause every future firing of a schedule until `resume_schedule`, or,
    with `until`, until that moment (then firings run normally again).

    Returns None for an unknown schedule. `until` must be in the future (a
    naive value is UTC); a past one raises `ValueError`. Pausing an already
    paused schedule keeps the original `paused_at`/`paused_by` (the pause
    began then, by them) and writes no audit row when nothing changes, so
    repeated clicks cannot rewrite history or flood the log. Repeating the
    pause with a different `until` (or none, making it indefinite) updates
    only the end time and audits it as `pause_schedule` with
    `updated: true`. A pause whose `until` already elapsed is cleared first
    (audited as an automatic resume) and the new pause starts fresh.

    celery-manager does not own beat, so beat keeps firing; what a pause
    changes is what the `@track` decorator does when the firing reaches the
    worker — it records the run as `skipped` and never calls the body (see
    `celery_manager.decorator._schedule_is_paused`). Tasks without `@track`
    are not affected.
    """
    now = datetime.now(timezone.utc)
    if until is not None:
        until = _as_aware_utc(until).astimezone(timezone.utc)
        if until <= now:
            raise ValueError("`until` must be in the future.")
    row = get_periodic_schedule(session, schedule_name)
    if row is None:
        return None
    _expire_if_stale(session, row, now)
    if row.paused_at is not None:
        previous = _as_aware_utc(row.paused_until) if row.paused_until is not None else None
        if previous == until:
            return row
        row.paused_until = until
        session.commit()
        record_audit(
            session, username, "pause_schedule", "schedule", schedule_name,
            {
                "updated": True,
                "until": until.isoformat() if until else None,
                "previous_until": previous.isoformat() if previous else None,
            },
        )
        return row
    row.paused_at = now
    row.paused_by = username
    row.paused_until = until
    session.commit()
    record_audit(
        session, username, "pause_schedule", "schedule", schedule_name,
        {"until": until.isoformat()} if until else None,
    )
    return row


def resume_schedule(session, schedule_name: str, username: str) -> PeriodicSchedule | None:
    """Undo `pause_schedule`. Returns None for an unknown schedule.

    Resuming does not run the task now and does not touch beat's own
    interval clock: the next regular beat firing simply runs normally.
    Firings missed while paused are not caught up — they already happened,
    and were recorded as `skipped`. Idempotent: resuming a schedule that is
    not paused is a no-op with no audit row (a pause that already timed out
    is cleared and audited as automatic, not attributed to `username`). The
    audit row records when the pause began and who started it, since
    resuming clears both columns.
    """
    row = get_periodic_schedule(session, schedule_name)
    if row is None:
        return None
    _expire_if_stale(session, row)
    if row.paused_at is None:
        return row
    details = {"paused_at": _as_aware_utc(row.paused_at).isoformat(), "paused_by": row.paused_by}
    if row.paused_until is not None:
        details["paused_until"] = _as_aware_utc(row.paused_until).isoformat()
    row.paused_at = None
    row.paused_by = None
    row.paused_until = None
    session.commit()
    record_audit(session, username, "resume_schedule", "schedule", schedule_name, details)
    return row


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
        # `paused_at`/`paused_by` are deliberately not touched: they are an
        # operator decision (`pause_schedule`), not part of the beat config,
        # and resetting them here silently un-paused every schedule on each
        # dashboard restart. The same goes for `schedule_override` /
        # `edited_by` / `edited_at` (`edit_schedule`): `schedule_spec` is
        # always the code's schedule, the edit sits beside it.
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
    apply_edits: bool = True,
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

    # The operator's edit, if any (and `apply_edits`): see `effective_schedule`.
    celery_schedule = _entry_schedule(entry, periodic.schedule_override if apply_edits else None, app=celery_app)
    anchor = _last_observed_run_at(session, schedule_name) if _is_interval_schedule(celery_schedule) else None
    occurrences = upcoming_occurrences(periodic, celery_schedule, count=count, from_time=from_time, anchor=anchor)
    pool = [o for o in list_overrides(session, schedule_name) if not o.consumed]
    window = timedelta(seconds=eta_tolerance_seconds)
    _expire_if_stale(session, periodic)
    paused_at = periodic.paused_at
    paused_until = _as_aware_utc(periodic.paused_until) if periodic.paused_until is not None else None

    windows = enabled_windows(session)

    result = []
    for t in occurrences:
        match = _find_within_tolerance(pool, t, window)
        if match is not None:
            pool.remove(match)
        result.append({
            "target_time": t,
            "action": match.action if match is not None else None,
            "consumed": match.consumed if match is not None else False,
            # True when the whole schedule is paused (`pause_schedule`) and
            # this firing falls before `paused_until` (or the pause is
            # indefinite): it will be recorded as skipped however `action`
            # reads. Occurrences at/after `paused_until` run normally.
            "paused": paused_at is not None and (paused_until is None or t < paused_until),
            # Name of the maintenance window this firing falls in (it will be
            # recorded as skipped), else None; several overlapping windows
            # are joined with ", ".
            "maintenance_window": ", ".join(w.name for w in windows_active_at(windows, schedule_name, t)) or None,
        })
    return result


def list_periodic_schedules(session) -> list[PeriodicSchedule]:
    """Every configured periodic schedule, for the dashboard's schedule-list
    screen — populated by sync_periodic_schedules() at create_app startup.
    Reads, apart from lazily clearing pauses whose `paused_until` passed."""
    rows = session.query(PeriodicSchedule).order_by(PeriodicSchedule.name).all()
    now = datetime.now(timezone.utc)
    for row in rows:
        _expire_if_stale(session, row, now)
    return rows


# --- editing a schedule's timing --------------------------------------------


def _schedule_target(session, celery_app, schedule_name: str):
    """`(row, beat_schedule entry)`, or None if either is missing: an edit
    needs both the stored row (it holds the edit) and the code's entry (the
    task it runs, and what "revert to code" goes back to)."""
    row = get_periodic_schedule(session, schedule_name)
    entry = celery_app.conf.beat_schedule.get(schedule_name)
    if row is None or entry is None:
        return None
    return row, entry


def preview_schedule(
    session,
    celery_app,
    schedule_name: str,
    data,
    count: int = 5,
    from_time: datetime | None = None,
) -> list[datetime] | None:
    """The next `count` runs `schedule_name` would have under the spec
    `data`, without saving anything. None for an unknown schedule;
    `ScheduleSpecError` (a sentence for a person) for an invalid spec.

    Projected exactly as `list_upcoming_with_overrides` will project it once
    saved: an interval from the schedule's last observed run, because
    `DatabaseScheduler` keeps beat's `last_run_at` when it swaps the
    schedule, so the first run after the edit is "last run + new interval",
    not "now + new interval"."""
    target = _schedule_target(session, celery_app, schedule_name)
    if target is None:
        return None
    row, _entry = target
    _spec, celery_schedule = validate_spec(data, app=celery_app, now=from_time)
    anchor = _last_observed_run_at(session, schedule_name) if _is_interval_schedule(celery_schedule) else None
    return upcoming_occurrences(row, celery_schedule, count=count, from_time=from_time, anchor=anchor)


def _occurs_at(row, celery_schedule, anchor, t: datetime, window: timedelta) -> bool:
    first = upcoming_occurrences(row, celery_schedule, count=1, from_time=t - window, anchor=anchor)
    return bool(first) and abs(first[0] - t) <= window


def _unmatched_overrides(session, row, celery_schedule, eta_tolerance_seconds: int) -> list[datetime]:
    """Future skips/cancels of this schedule that no run of `celery_schedule`
    lines up with any more (same windows as the decorator's matching), so
    they will not apply. Reported, not deleted: see `edit_schedule`."""
    now = datetime.now(timezone.utc)
    if _is_interval_schedule(celery_schedule):
        anchor = _last_observed_run_at(session, row.name)
        window = min(timedelta(seconds=eta_tolerance_seconds), celery_schedule.run_every / 2)
    else:
        anchor, window = None, CALENDAR_MATCH_WINDOW
    unmatched = []
    for override in list_overrides(session, row.name):
        target_time = _as_aware_utc(override.target_time)
        if override.consumed or target_time <= now:
            continue
        if not _occurs_at(row, celery_schedule, anchor, target_time, window):
            unmatched.append(target_time)
    return unmatched


def _stored_spec(row: PeriodicSchedule) -> dict | None:
    try:
        return load_spec(row.schedule_override)
    except ScheduleSpecError:
        return None


def edit_schedule(
    session,
    celery_app,
    schedule_name: str,
    data,
    username: str,
    eta_tolerance_seconds: int = 60,
):
    """Save an operator's edit of when `schedule_name` runs.

    Returns `(row, unmatched)` where `unmatched` lists the future
    skips/cancels that no run of the new schedule lines up with; None for
    an unknown schedule. Raises `ScheduleSpecError` for an invalid spec
    (rejected by Celery's own `crontab(...)` parser, a crontab that never
    fires, an interval under `MIN_INTERVAL_SECONDS`).

    The code's `beat_schedule` stays the default: the edit is stored beside
    it and `revert_schedule` removes it. Beat applies it only when it runs
    `celery_manager.beat:DatabaseScheduler`; the API refuses edits unless
    `CeleryManagerConfig.schedule_editing` says it does.

    Existing skips/cancels are kept, even ones the new schedule no longer
    has a run for. They then never match (the decorator matches the
    occurrence beat stamps), so they cannot skip the wrong run, and a
    revert to code brings them back into force — deleting an operator's
    decision as a side effect of another edit would be the surprise. Their
    times are returned and written to the audit row so the dashboard can
    say so. Saving the same spec again is a no-op with no audit row.
    """
    target = _schedule_target(session, celery_app, schedule_name)
    if target is None:
        return None
    row, _entry = target
    spec, celery_schedule = validate_spec(data, app=celery_app)
    before = _stored_spec(row)
    unmatched = _unmatched_overrides(session, row, celery_schedule, eta_tolerance_seconds)
    if before == spec:
        return row, unmatched
    row.schedule_override = dump_spec(spec)
    row.edited_by = username
    row.edited_at = datetime.now(timezone.utc)
    session.commit()
    record_audit(
        session, username, "edit_schedule", "schedule", schedule_name,
        {
            "before": before,
            "after": spec,
            "code": row.schedule_spec,
            "unmatched_overrides": [t.isoformat() for t in unmatched],
        },
    )
    return row, unmatched


def revert_schedule(session, schedule_name: str, username: str) -> PeriodicSchedule | None:
    """Drop the operator's edit: the schedule runs as the code says again
    (beat picks it up on its next refresh). None for an unknown schedule.
    Idempotent: reverting a schedule with no edit writes no audit row."""
    row = get_periodic_schedule(session, schedule_name)
    if row is None:
        return None
    if row.schedule_override is None:
        return row
    details = {
        "before": _stored_spec(row) or row.schedule_override,
        "code": row.schedule_spec,
        "edited_by": row.edited_by,
        "edited_at": _as_aware_utc(row.edited_at).isoformat() if row.edited_at else None,
    }
    row.schedule_override = None
    row.edited_by = None
    row.edited_at = None
    session.commit()
    record_audit(session, username, "revert_schedule", "schedule", schedule_name, details)
    return row


def schedule_edit_fields(row: PeriodicSchedule, celery_app, apply_edits: bool = True) -> dict:
    """The editing half of a schedule's API summary.

    `schedule_spec` is the schedule in force (the edit's description while
    one applies), `code_schedule_spec` always the code's. `code_schedule` is
    the code's schedule as a spec, to prefill the editor (None for solar and
    custom schedule kinds, which a spec cannot express)."""
    entry = celery_app.conf.beat_schedule.get(row.name)
    spec = _stored_spec(row)
    code_schedule = spec_from_schedule(_entry_schedule(entry)) if entry is not None else None
    effective = row.schedule_spec
    if spec is not None and apply_edits and entry is not None:
        effective = str(_entry_schedule(entry, row.schedule_override, app=celery_app))
    return {
        "schedule_spec": effective,
        "code_schedule_spec": row.schedule_spec,
        "code_schedule": code_schedule,
        "schedule_override": spec,
        "edited_by": row.edited_by,
        "edited_at": _as_aware_utc(row.edited_at) if row.edited_at else None,
    }
