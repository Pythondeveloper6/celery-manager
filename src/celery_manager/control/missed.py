"""Missed-run detection: scheduled firings that were due but never arrived.

The beat scheduler mixin (`celery_manager.beat`) stamps every firing with the
time it was due (`task_runs.occurrence_at`) and when beat sent it
(`task_runs.beat_sent_at`). Comparing what a schedule *should* have produced
with the stamps that did arrive shows a firing that vanished: beat was down,
the message was lost, or no worker took it.

Everything here is a pure read: no audit rows, no writes, no pause-expiry
side effects. The alerting evaluator calls `list_missed_runs` and
`missed_run_counts` directly, so keep it that way.

Rules, shared by both public functions:

- **Judged only from the first stamp onward.** Before beat stamped anything
  for a schedule, a missing run cannot be told from an unstamped one, so the
  earliest `occurrence_at` of that schedule, run or beat firing, is where
  judging starts. With neither, and beat recording to this database (it has
  written a heartbeat), judging starts when the schedule was first seen by
  the backend sync, so a schedule that never fired is reported after one
  period plus grace. Otherwise detection is inactive.
- **Sent** means beat wrote a `schedule_firings` row; a firing with no row
  and no stamped run was never sent (`kind: "never_sent"`). A sent firing
  with no `task_runs` row for its task id was never received
  (`kind: "never_arrived"`), judged only for schedules that have a stamped
  run, since an untracked task writes none.
- **Arrived means a `task_runs` row exists.** Its status does not matter:
  skipped (including whole-schedule pauses), revoked and failed runs all
  reached the worker. Tasks outside `@track` / `TrackedTask` write no rows.
- **Grace.** A firing is only reported once `grace_seconds` have passed since
  it was due, so a slow broker or a busy worker is not a false alarm.
- **crontab / solar**: the expected firings are recomputed from the schedule
  (the same `next_occurrence_after` step beat uses to stamp them). A firing
  is missed if no stamped run has that `occurrence_at` (within
  `CALENDAR_MATCH_WINDOW`, the tolerance the rest of the codebase uses for
  calendar occurrences, to absorb sub-second rounding).
- **Interval schedules** have no fixed grid, so they are judged from the
  chain of stamps: beat fires the next run at `previous beat_sent_at + k *
  run_every` (k > 1 after downtime, when it fires once for the latest point).
  The `k - 1` slots in between were never sent. After the last stamped run,
  every slot up to `now - grace` is missing.
- **Schedule edits.** While an operator's edit is in force
  (`control.schedules.edit_schedule`, `apply_edits`), the edited schedule
  is what beat fires, so it is what is judged, and only from `edited_at`
  on: firings before the edit followed a schedule that is no longer known.
- **Bounded work.** The lookback defaults to `DEFAULT_LOOKBACK` and the walk
  over expected firings stops after `MAX_EXPECTED_STEPS`; at most
  `max_results` of the newest missed firings are returned.
"""

from __future__ import annotations

import itertools
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func

from celery_manager.control.schedules import _entry_schedule
from celery_manager.db.models import (
    AppSetting,
    PeriodicSchedule,
    ScheduleFiring,
    TaskRun,
)
from celery_manager.occurrences import (
    CALENDAR_MATCH_WINDOW,
    as_aware_utc,
    is_interval_schedule,
    next_occurrence_after,
)
from celery_manager.schedulers.service import entries_for

DEFAULT_LOOKBACK = timedelta(days=7)
DEFAULT_GRACE_SECONDS = 300
# An every-minute crontab over the default week is ~10k steps.
MAX_EXPECTED_STEPS = 20_000
DEFAULT_MAX_RESULTS = 500

__all__ = [
    "first_stamped_occurrence",
    "list_missed_runs",
    "missed_run_counts",
]


def first_stamped_occurrence(session, schedule_name: str) -> datetime | None:
    """Detection start: earliest stamped run/firing, or first backend sighting
    when beat records to this database."""
    values = [
        session.query(func.min(TaskRun.occurrence_at)).filter(TaskRun.schedule_name == schedule_name).scalar(),
        session.query(func.min(ScheduleFiring.occurrence_at)).filter(ScheduleFiring.schedule_name == schedule_name).scalar(),
    ]
    values = [as_aware_utc(v) for v in values if v is not None]
    return min(values) if values else _first_seen(session, schedule_name)


def _first_seen(session, schedule_name: str) -> datetime | None:
    """When the backend sync first saw the schedule, but only while beat
    records firings here (else a never-stamped schedule is not judged)."""
    if session.get(AppSetting, "beat_last_tick") is None:
        return None
    value = session.query(PeriodicSchedule.first_seen_at).filter_by(name=schedule_name).scalar()
    return as_aware_utc(value) if value is not None else None


def list_missed_runs(
    session,
    celery_app,
    schedule_name: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    now: datetime | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    apply_edits: bool = True,
    entry: dict | None = None,
) -> list[dict]:
    """Firings of `schedule_name` that were due in `[since, until]` and never
    arrived, oldest first, as `{"due_at": datetime, "kind": "never_sent" |
    "never_arrived"}` (aware UTC).

    `since` defaults to `now - DEFAULT_LOOKBACK` and is clamped to the first
    stamped occurrence; `until` defaults to `now` and is clamped to
    `now - grace_seconds`. Returns `[]` for a schedule that is not in the
    host app's `beat_schedule` or has no stamped run yet. Pure read.
    `apply_edits` is as for `control.schedules.effective_schedule`. A
    disabled entry has no missed runs, and a re-enabled one is judged from
    when it was re-enabled. Pass `entry` when the caller already has it.
    """
    now = as_aware_utc(now) if now is not None else datetime.now(timezone.utc)
    if entry is None:
        entry = entries_for(session, celery_app).get(schedule_name)
    if entry is None or not entry.get("enabled", True):
        return []
    first = first_stamped_occurrence(session, schedule_name)
    if first is None:
        return []

    since = as_aware_utc(since) if since is not None else now - DEFAULT_LOOKBACK
    since = max(since, first)
    override, edited_at = None, None
    row = (
        session.query(PeriodicSchedule.schedule_override, PeriodicSchedule.edited_at, PeriodicSchedule.enabled_changed_at)
        .filter_by(name=schedule_name)
        .one_or_none()
    )
    if apply_edits and row is not None and row.schedule_override:
        override, edited_at = row.schedule_override, row.edited_at
    for changed in (edited_at, row.enabled_changed_at if row is not None else None):
        if changed is not None:
            since = max(since, as_aware_utc(changed))
    upper = now - timedelta(seconds=grace_seconds)
    if until is not None:
        upper = min(upper, as_aware_utc(until))
    if upper < since:
        return []

    celery_schedule = _entry_schedule(entry, override, app=celery_app)
    if is_interval_schedule(celery_schedule):
        missed = _missed_interval(
            session, schedule_name, celery_schedule.run_every, first, since, upper, grace_seconds, max_results
        )
    else:
        missed = _missed_calendar(
            session, schedule_name, celery_schedule, first, since, upper, max_results
        )
    found = [{"due_at": t, "kind": "never_sent"} for t in missed]
    found += [{"due_at": t, "kind": "never_arrived"} for t in _never_arrived(session, schedule_name, since, upper)]
    return sorted(found, key=lambda m: m["due_at"])[-max_results:]


def missed_run_counts(
    session,
    celery_app,
    since: datetime,
    *,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    now: datetime | None = None,
    apply_edits: bool = True,
) -> dict[str, int]:
    """`{schedule name: missed firings since `since`}` for every schedule in
    `beat_schedule` that has at least one missed firing (zero counts are
    omitted). Same rules as `list_missed_runs`; pure read. Counts are capped
    at `DEFAULT_MAX_RESULTS` per schedule."""
    counts: dict[str, int] = {}
    for name, entry in entries_for(session, celery_app).items():
        missed = list_missed_runs(
            session, celery_app, name, since=since, grace_seconds=grace_seconds, now=now,
            apply_edits=apply_edits, entry=entry,
        )
        if missed:
            counts[name] = len(missed)
    return counts


def _stamped_occurrences(session, schedule_name: str, start: datetime, end: datetime) -> list[datetime]:
    rows = []
    for model in (TaskRun, ScheduleFiring):
        rows += (
            session.query(model.occurrence_at)
            .filter(
                model.schedule_name == schedule_name,
                model.occurrence_at.isnot(None),
                model.occurrence_at >= start,
                model.occurrence_at <= end,
            )
            .distinct()
            .all()
        )
    return sorted({as_aware_utc(r[0]) for r in rows})


def _never_arrived(session, name, since, upper) -> list[datetime]:
    """Sent firings in the window whose task id has no `task_runs` row. Only
    judged where the schedule has stamped runs: an untracked task has none."""
    if session.query(TaskRun.id).filter(TaskRun.schedule_name == name, TaskRun.occurrence_at.isnot(None)).first() is None:
        return []
    rows = (
        session.query(ScheduleFiring.occurrence_at)
        .filter(
            ScheduleFiring.schedule_name == name,
            ScheduleFiring.task_id.isnot(None),
            ScheduleFiring.occurrence_at >= since,
            ScheduleFiring.sent_at <= upper,
            ~exists().where(TaskRun.task_id == ScheduleFiring.task_id),
        )
        .all()
    )
    return sorted({as_aware_utc(r[0]) for r in rows})


def _missed_calendar(session, name, celery_schedule, first, since, upper, max_results) -> list[datetime]:
    # Pad the window by the match tolerance so a stamp a hair outside it
    # still matches the expected point next to it.
    arrived = _stamped_occurrences(
        session, name, since - CALENDAR_MATCH_WINDOW, upper + CALENDAR_MATCH_WINDOW
    )
    missed: deque[datetime] = deque(maxlen=max_results)
    # Strictly after `first` (that firing itself arrived); `since` is
    # inclusive, so step from just before it.
    cursor = max(first, since - CALENDAR_MATCH_WINDOW)
    i = 0  # `arrived` is sorted and expected points ascend: a moving pointer
    for _ in range(MAX_EXPECTED_STEPS):
        nxt = as_aware_utc(next_occurrence_after(celery_schedule, cursor))
        if nxt <= cursor or nxt > upper:
            break
        cursor = nxt
        if cursor < since:
            continue
        while i < len(arrived) and arrived[i] < cursor - CALENDAR_MATCH_WINDOW:
            i += 1
        if i < len(arrived) and abs(arrived[i] - cursor) <= CALENDAR_MATCH_WINDOW:
            continue
        missed.append(cursor)
    return list(missed)


def _missed_interval(session, name, run_every, first, since, upper, grace_seconds, max_results) -> list[datetime]:
    if run_every <= timedelta(0):
        return []
    # The chain of (due time, beat-sent time) pairs, one per distinct stamped
    # firing. Include the last firing before `since` as the chain's base so
    # a gap that straddles `since` is still measured.
    pairs = []
    for model, sent in ((TaskRun, TaskRun.beat_sent_at), (ScheduleFiring, ScheduleFiring.sent_at)):
        base = (
            session.query(model.occurrence_at, sent)
            .filter(model.schedule_name == name, model.occurrence_at.isnot(None), model.occurrence_at < since)
            .order_by(model.occurrence_at.desc())
            .first()
        )
        pairs += ([tuple(base)] if base is not None else []) + [
            tuple(r) for r in session.query(model.occurrence_at, sent)
            .filter(model.schedule_name == name, model.occurrence_at.isnot(None), model.occurrence_at >= since)
            .all()
        ]
    chain: list[tuple[datetime, datetime]] = []
    seen: set[datetime] = set()
    for occurrence, sent in sorted(pairs, key=lambda p: as_aware_utc(p[0])):
        occurrence = as_aware_utc(occurrence)
        if occurrence in seen:
            continue
        seen.add(occurrence)
        # An older stamp without beat_sent_at: the due time is the best anchor.
        chain.append((occurrence, as_aware_utc(sent) if sent is not None else occurrence))
    if not chain:
        # Never stamped nor fired: count periods from when the schedule was first seen.
        chain.append((first, first))

    grace = timedelta(seconds=grace_seconds)
    missed: set[datetime] = set()

    def add_slots(anchor: datetime, count: int | None) -> None:
        # Slots anchor + j*run_every, j >= 1, inside [since, upper]. Jump
        # straight to the first slot in range so a long gap is not walked.
        j = max(1, -((anchor - since) // run_every))  # ceil((since - anchor) / run_every)
        steps = 0
        while (count is None or j <= count) and steps < MAX_EXPECTED_STEPS:
            slot = anchor + j * run_every
            if slot > upper:
                break
            if slot >= since:
                missed.add(slot)
            j += 1
            steps += 1

    for (_, prev_sent), (occurrence, _) in itertools.pairwise(chain):
        gap = occurrence - prev_sent
        if gap > run_every + grace:
            # Beat fires the latest due point once after a gap, so the
            # points before the stamped one never went out. round(), not
            # floor: a lost message shifts the next slot by beat's send lag.
            add_slots(prev_sent, round(gap / run_every) - 1)
    # After the newest stamped run: everything that should have been sent by
    # now (less grace) and was not.
    add_slots(chain[-1][1], None)
    return sorted(missed)[-max_results:]
