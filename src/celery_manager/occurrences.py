"""Occurrence identity: which firing of a periodic schedule a message is.

Shared by the beat side (`celery_manager.beat`, which stamps it onto every
message beat publishes) and the worker side (`celery_manager.decorator`,
which reads it back to decide whether a skip/cancel applies). The dashboard's
occurrence list (`control.schedules.upcoming_occurrences`) uses the same
`next_occurrence_after` step, so a time shown there and the time beat stamps
for that firing come from one computation.

Deliberately free of database imports: the beat process may not have
celery-manager's storage configured at all.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from celery.schedules import crontab, solar
from celery.schedules import schedule as interval_schedule

# Message headers set by the beat scheduler mixin.
SCHEDULE_HEADER = "cm_schedule"  # beat entry name (the `beat_schedule` key)
OCCURRENCE_HEADER = "cm_occurrence"  # nominal due time of this firing, UTC ISO 8601
INTERVAL_HEADER = "cm_interval"  # seconds, only for fixed-interval schedules
SENT_AT_HEADER = (
    "cm_sent_at"  # when beat sent it: the anchor for the next interval firing
)

# Calendar schedules (crontab, solar) are matched to an override this
# closely. Both sides compute the same value; the window only absorbs
# rounding when a timestamp round-trips through the browser (milliseconds).
CALENDAR_MATCH_WINDOW = timedelta(seconds=1)

# Upper bound when walking a calendar schedule forward to the latest due
# point after a long beat outage (an every-minute crontab down for a week
# is ~10k steps). Past it we stop and use what we reached.
_MAX_CATCH_UP_STEPS = 20_000


def is_interval_schedule(celery_schedule) -> bool:
    """True for a fixed-interval `schedule(run_every=...)` (what a bare number
    or `timedelta` entry normalizes to), False for crontab/solar."""
    return isinstance(celery_schedule, interval_schedule) and not isinstance(
        celery_schedule, (crontab, solar)
    )


def as_aware_utc(dt: datetime) -> datetime:
    """Naive datetimes in this codebase are UTC (SQLite drops tzinfo)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def next_occurrence_after(celery_schedule, t: datetime) -> datetime:
    """The schedule's next firing strictly after `t`, as beat would compute it
    with `last_run_at = t`.

    `remaining_estimate()` measures against the schedule's own `now()`, so we
    pin `nowfun` to `t` on a private shallow copy (the host's schedule objects
    are shared, so they must not be mutated)."""
    local = copy.copy(celery_schedule)
    local.nowfun = lambda: t
    return t + local.remaining_estimate(t)


def due_occurrence(
    celery_schedule, last_run_at: datetime | None, now: datetime
) -> datetime:
    """The occurrence beat is firing at `now`, given the entry's previous
    `last_run_at`: the latest scheduled point that is due (<= `now`).

    Normally that is simply the next point after `last_run_at`. After beat
    downtime several points may have passed; beat fires once, and we name the
    firing after the most recent of them (the one the dashboard showed last).
    For interval schedules the points are `last_run_at + k * run_every`, which
    is what the dashboard projects from the previous firing's `cm_sent_at`.
    """
    now = as_aware_utc(now)
    if last_run_at is None:
        return now
    last_run_at = as_aware_utc(last_run_at)

    if is_interval_schedule(celery_schedule):
        run_every = celery_schedule.run_every
        if run_every <= timedelta(0):
            return now
        k = max((now - last_run_at) // run_every, 1)
        return last_run_at + k * run_every

    point = as_aware_utc(next_occurrence_after(celery_schedule, last_run_at))
    for _ in range(_MAX_CATCH_UP_STEPS):
        following = as_aware_utc(next_occurrence_after(celery_schedule, point))
        if following > now or following <= point:
            break
        point = following
    return point


def format_occurrence(dt: datetime) -> str:
    return as_aware_utc(dt).isoformat()


def parse_occurrence(value) -> datetime | None:
    """Parse a header timestamp; None for anything malformed."""
    if isinstance(value, datetime):
        return as_aware_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_aware_utc(datetime.fromisoformat(value))
    except ValueError:
        return None
