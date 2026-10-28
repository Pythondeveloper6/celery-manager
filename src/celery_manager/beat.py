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

Schedule edits from the dashboard need one more thing: beat must read them.
`DatabaseScheduler` is `Scheduler` plus that::

    app.conf.celery_manager_db_url = "postgresql://..."  # celery-manager's db_url
    celery -A yourapp beat -S celery_manager.beat:DatabaseScheduler

It reads the saved edits (`periodic_schedules.schedule_override`) at start
and every `app.conf.celery_manager_schedule_refresh` seconds (default 10),
swapping the affected entries' schedule in place: `last_run_at` is kept, so
an interval edit fires at "last run + new interval" and a crontab edit at
its next matching time, with no beat restart. `Scheduler` stays DB-free.
"""

from __future__ import annotations

import logging
import time
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
from celery_manager.schedule_specs import ScheduleSpecError, build_schedule, load_spec

logger = logging.getLogger(__name__)

__all__ = ["DatabaseScheduler", "Scheduler", "StampOccurrencesMixin", "occurrence_headers"]


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

    def tick(self, *args, **kwargs):
        from celery_manager.beat_health import record_tick
        interval = record_tick(self)
        delay = super().tick(*args, **kwargs)
        return min(delay, interval) if interval else delay

    def close(self):
        try:
            return super().close()
        finally:
            if getattr(self, "_cm_health_engine", None) is not None:
                self._cm_health_engine.dispose()

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


# Seconds between reads of the saved edits, unless
# `app.conf.celery_manager_schedule_refresh` says otherwise. An edit reaches
# beat within this long; each read is one small query.
DEFAULT_REFRESH_SECONDS = 10.0


class DatabaseScheduler(Scheduler):
    """`Scheduler` that also applies schedule edits saved from the dashboard.

    The code's `beat_schedule` stays the source of every entry (task, args,
    options) and the default timing; an edit only replaces an entry's
    `schedule`, and "revert to code" puts the code's back. Needs
    celery-manager's database: `app.conf.celery_manager_db_url` (the same
    URL as `CeleryManagerConfig.db_url`). Beat only reads it, and never
    migrates it; start the API (or run `celery-manager migrate`) first.

    If the database can't be read, beat logs it and keeps firing with what
    it last applied (the code's schedules at start), retrying on every
    refresh: a database outage must not stop scheduled work.

    Works on top of `PersistentScheduler`'s shelve file. Beat's state there
    (`last_run_at`) survives an edit and a restart; the stored schedule is
    reset to the code's by Celery at start and the edit re-applied from the
    database, so the database is the one place an edit lives.
    django-celery-beat keeps its own schedules in its own tables and is not
    combined with this: edit those in the Django admin instead.
    """

    def __init__(self, *args, **kwargs):
        app = kwargs.get("app", args[0] if args else None)
        db_url = app.conf.get("celery_manager_db_url") if app is not None else None
        if not db_url:
            raise RuntimeError(
                "celery_manager.beat:DatabaseScheduler needs celery-manager's database: "
                "set app.conf.celery_manager_db_url to the same URL as CeleryManagerConfig.db_url."
            )
        self._cm_db_url = db_url
        self._cm_engine = None
        self._cm_refresh_seconds = float(
            app.conf.get("celery_manager_schedule_refresh") or DEFAULT_REFRESH_SECONDS
        )
        self._cm_last_refresh: float | None = None
        # Entry name -> the stored edit text in force (None: the code's).
        self._cm_applied: dict[str, str | None] = {}
        super().__init__(*args, **kwargs)

    def setup_schedule(self):
        super().setup_schedule()
        # Celery just merged `beat_schedule` into every entry, resetting
        # each schedule to the code's: nothing of ours is applied any more.
        self._cm_applied = {}
        self.refresh_schedule_edits()

    def tick(self, *args, **kwargs):
        now = time.monotonic()
        if self._cm_last_refresh is None or now - self._cm_last_refresh >= self._cm_refresh_seconds:
            self.refresh_schedule_edits()
        delay = super().tick(*args, **kwargs)
        # Never sleep past the next refresh: an hourly entry edited to run
        # every minute must not wait out the hour beat was sleeping for.
        until_refresh = max(self._cm_refresh_seconds - (time.monotonic() - self._cm_last_refresh), 0)
        return min(delay, until_refresh)

    def _cm_read_edits(self) -> dict[str, str | None]:
        from sqlalchemy import select

        from celery_manager.db.models import PeriodicSchedule
        from celery_manager.db.session import create_db_engine

        if self._cm_engine is None:
            self._cm_engine = create_db_engine(self._cm_db_url)
        with self._cm_engine.connect() as conn:
            rows = conn.execute(select(PeriodicSchedule.name, PeriodicSchedule.schedule_override))
            return {name: override for name, override in rows}

    def _cm_schedule_for(self, name: str, override: str | None):
        """The schedule object entry `name` should have, or None to leave it
        alone (an entry that is not in `beat_schedule`, such as Celery's own
        `celery.backend_cleanup`)."""
        if override:
            try:
                spec = load_spec(override)
                if spec is not None:
                    return build_schedule(spec, app=self.app)
            except ScheduleSpecError:
                logger.warning("celery-manager: ignoring an invalid schedule edit for %r: %r", name, override)
        code = self.app.conf.beat_schedule.get(name)
        if code is None:
            return None
        return self.Entry(**dict(code, name=name, app=self.app)).schedule

    def refresh_schedule_edits(self) -> bool:
        """Read the saved edits and apply any that changed since the last
        read. Returns True if an entry's schedule changed."""
        self._cm_last_refresh = time.monotonic()
        try:
            edits = self._cm_read_edits()
        except Exception:
            logger.exception("celery-manager: could not read schedule edits; keeping the current schedules")
            return False
        changed = False
        for name, entry in list(self.schedule.items()):
            override = edits.get(name)
            if self._cm_applied.get(name) == override:
                continue
            new_schedule = self._cm_schedule_for(name, override)
            if new_schedule is None:
                continue
            # A new entry rather than a mutation: Celery compares a copy of
            # the previous entries to notice changes, and the copy shares the
            # entry objects. `dict(entry)` carries `last_run_at` and
            # `total_run_count` over, as Celery's own `entry.next()` does.
            self.schedule[name] = entry.__class__(**dict(entry, schedule=new_schedule))
            self._cm_applied[name] = override
            changed = True
            logger.info(
                "celery-manager: schedule %r now runs %s (%s)",
                name, new_schedule, "edited in the dashboard" if override else "as in the code",
            )
        if changed:
            self._heap = None  # rebuilt from the new schedules on this tick
        return changed


def __getattr__(name):
    # Optional schedulers must not make importing celery_manager require either
    # Django settings or a RedBeat installation. Keep DatabaseScheduler's
    # existing public meaning (the package's static-schedule editor).
    if name == "RedBeatScheduler":
        from redbeat.schedulers import RedBeatScheduler
        cls = type(name, (StampOccurrencesMixin, RedBeatScheduler), {"__module__": __name__})
    elif name == "DjangoDatabaseScheduler":
        from django_celery_beat.schedulers import DatabaseScheduler
        cls = type(name, (StampOccurrencesMixin, DatabaseScheduler), {"__module__": __name__})
    else:
        raise AttributeError(name)
    globals()[name] = cls
    return cls
