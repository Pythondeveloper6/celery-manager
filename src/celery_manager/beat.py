"""Beat scheduler support: stamp each firing with its occurrence identity.

Run beat with the bundled scheduler::

    celery -A yourapp beat -S celery_manager.beat:Scheduler

or set ``app.conf.beat_scheduler = "celery_manager.beat:Scheduler"``. If you
already use another scheduler (django-celery-beat, RedBeat, ...), mix
`StampOccurrencesMixin` in front of it::

    from django_celery_beat.schedulers import DatabaseScheduler
    from celery_manager.beat import StampOccurrencesMixin

    class Scheduler(StampOccurrencesMixin, DatabaseScheduler):
        pass

Every message beat publishes then carries these headers:

- ``cm_schedule``: the beat entry name.
- ``cm_occurrence``: the scheduled time of this firing (UTC ISO 8601),
  computed from the schedule, not the wall-clock send time. It is the same
  value the dashboard lists for that occurrence.
- ``cm_interval``: the period in seconds, for interval schedules only.
- ``cm_sent_at``: when beat sent it (anchors the next interval occurrence).

The `@track` decorator uses them to apply a skip or cancel to exactly the
firing it was saved for, however late the task starts. Messages without them
(``.delay()``, ``apply_async``, dashboard retries) never consume an override.

The beat process does not need celery-manager's database or `setup()`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from celery.beat import PersistentScheduler

from celery_manager.occurrences import (
    INTERVAL_HEADER,
    OCCURRENCE_HEADER,
    SCHEDULE_HEADER,
    SENT_AT_HEADER,
    as_aware_utc,
    due_occurrence,
    format_occurrence,
    is_interval_schedule,
)

logger = logging.getLogger(__name__)

__all__ = ["Scheduler", "StampOccurrencesMixin", "occurrence_headers"]


def _schedule_now(celery_schedule) -> datetime:
    try:
        return as_aware_utc(celery_schedule.now())
    except Exception:  # noqa: BLE001 — fall back to the plain clock
        return datetime.now(timezone.utc)


def occurrence_headers(entry, sent_at: datetime | None = None) -> dict:
    """The celery-manager headers for firing `entry` now.

    `entry` is the entry as it was *before* beat advanced it, so its
    `last_run_at` is the previous firing. `sent_at` is the new `last_run_at`
    when known (it anchors the next interval occurrence)."""
    celery_schedule = entry.schedule
    now = _schedule_now(celery_schedule)
    sent_at = as_aware_utc(sent_at) if sent_at is not None else now
    last_run_at = entry.last_run_at
    if last_run_at is not None and last_run_at.tzinfo is None:
        # Naive beat timestamps are in the app's timezone, as Celery reads them.
        last_run_at = celery_schedule.maybe_make_aware(last_run_at)
    occurrence = due_occurrence(celery_schedule, last_run_at, now)
    headers = {
        SCHEDULE_HEADER: entry.name,
        OCCURRENCE_HEADER: format_occurrence(occurrence),
        SENT_AT_HEADER: format_occurrence(sent_at),
    }
    if is_interval_schedule(celery_schedule):
        headers[INTERVAL_HEADER] = celery_schedule.run_every.total_seconds()
    return headers


class _StampedEntry:
    """A read-only view of a schedule entry with extra message headers.

    Used instead of copying the entry, because entry classes differ between
    schedulers (django-celery-beat's `ModelEntry` cannot be rebuilt the way
    `ScheduleEntry` can) and the stored entry must not keep our headers."""

    def __init__(self, entry, headers: dict):
        self._entry = entry
        options = dict(entry.options or {})
        options["headers"] = {**(options.get("headers") or {}), **headers}
        self.options = options

    def __getattr__(self, name):
        return getattr(self._entry, name)


class StampOccurrencesMixin:
    """Scheduler mixin that adds occurrence headers to every beat message.

    Overrides `apply_async`, the one method every firing goes through (both
    `tick` -> `apply_entry` and a direct `apply_async(entry)` call). Header
    computation is guarded: if it fails, the task is still sent, unstamped.
    """

    def apply_async(self, entry, producer=None, advance=True, **kwargs):
        sent_at = None
        if advance:
            # Advance the stored entry ourselves so the parent never stores
            # the stamped view; `entry` stays the pre-advance one we need.
            sent_at = self.reserve(entry).last_run_at
        try:
            headers = occurrence_headers(entry, sent_at)
        except Exception:  # stamping must never stop a firing
            logger.exception(
                "celery-manager: could not compute occurrence headers for %r",
                entry.name,
            )
            return super().apply_async(
                entry, producer=producer, advance=False, **kwargs
            )
        return super().apply_async(
            _StampedEntry(entry, headers), producer=producer, advance=False, **kwargs
        )


class Scheduler(StampOccurrencesMixin, PersistentScheduler):
    """Celery's default `PersistentScheduler`, stamping occurrence headers."""
