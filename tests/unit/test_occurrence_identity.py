"""Explicit occurrence identity for per-occurrence skip/cancel.

Beat (with `celery_manager.beat`'s scheduler mixin) stamps each message with
the beat entry name and the scheduled time of the firing; the decorator
matches an override against that, not against the wall-clock start time.
Before this, a firing that sat in the queue longer than
`eta_tolerance_seconds` ran anyway, and a manual `.delay()` near the target
time consumed the skip so the real firing ran.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery
from celery.beat import ScheduleEntry
from celery.beat import Scheduler as CeleryScheduler
from celery.exceptions import Retry
from celery.schedules import crontab
from celery.schedules import schedule as interval_schedule
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager import decorator as decorator_module
from celery_manager.beat import Scheduler, StampOccurrencesMixin, occurrence_headers
from celery_manager.control.schedules import (
    list_upcoming_with_overrides,
    pause_occurrence,
    pause_schedule,
    sync_periodic_schedules,
)
from celery_manager.db.models import Base, ScheduleOverride, TaskRun
from celery_manager.decorator import configure, track
from celery_manager.occurrences import due_occurrence

NAME = "demo-every-2h"
EVERY = timedelta(hours=2)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _stamp(
    occurrence: datetime,
    name: str = NAME,
    interval: timedelta | None = None,
    sent_at=None,
) -> dict:
    headers = {
        "cm_schedule": name,
        "cm_occurrence": occurrence.isoformat(),
        "cm_sent_at": (sent_at or occurrence).isoformat(),
    }
    if interval is not None:
        headers["cm_interval"] = interval.total_seconds()
    return headers


@pytest.fixture
def env():
    app = Celery("test_app", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    app.conf.beat_schedule = {
        NAME: {"task": "demo.echo", "schedule": interval_schedule(run_every=EVERY)}
    }

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    configure(session_factory=Session, eta_tolerance_seconds=60)

    calls = []

    @app.task(name="demo.echo", bind=True)
    @track(schedule_name=NAME)
    def echo(self, value):
        calls.append(value)
        return value

    with Session() as s:
        sync_periodic_schedules(s, app)
    return app, Session, echo, calls


def _add_override(
    Session, target_time: datetime, name: str = NAME, action: str = "skip"
) -> None:
    with Session() as s:
        s.add(
            ScheduleOverride(
                schedule_name=name,
                target_time=target_time,
                action=action,
                consumed=False,
                created_by="alice",
                created_at=datetime.now(timezone.utc),
            )
        )
        s.commit()


def _consumed(Session, name: str = NAME) -> list[bool]:
    with Session() as s:
        return [
            o.consumed
            for o in s.query(ScheduleOverride)
            .filter_by(schedule_name=name)
            .order_by(ScheduleOverride.id)
        ]


# --- worker side -------------------------------------------------------------


def test_late_start_far_beyond_tolerance_is_still_skipped(env):
    _app, Session, echo, calls = env
    target = datetime.now(timezone.utc) - timedelta(
        hours=1
    )  # sat in the queue for an hour
    _add_override(Session, target)

    echo.apply(args=["late"], headers=_stamp(target, interval=EVERY))

    assert calls == []
    assert _consumed(Session) == [True]
    with Session() as s:
        run = s.query(TaskRun).one()
        assert run.status == "skipped"


def test_manual_call_near_target_does_not_consume_the_skip(env):
    _app, Session, echo, calls = env
    target = datetime.now(timezone.utc)
    _add_override(Session, target)
    # Beat has stamped before (a previous firing, recorded in storage).
    with Session() as s:
        s.add(
            TaskRun(
                task_id="earlier",
                task_name="demo.echo",
                status="succeeded",
                schedule_name=NAME,
                occurrence_at=target - EVERY,
                beat_sent_at=target - EVERY,
            )
        )
        s.commit()

    echo.apply(args=["manual"])  # `.delay()` at the target time: no headers
    assert calls == ["manual"]
    assert _consumed(Session) == [False]

    echo.apply(args=["beat"], headers=_stamp(target, interval=EVERY))  # the real firing
    assert calls == ["manual"]
    assert _consumed(Session) == [True]


def test_manual_call_after_a_stamped_message_in_this_process_does_not_consume(env):
    _app, Session, echo, calls = env
    now = datetime.now(timezone.utc)
    echo.apply(args=["beat"], headers=_stamp(now - EVERY, interval=EVERY))
    _add_override(Session, now)

    echo.apply(args=["manual"])
    assert calls == ["beat", "manual"]
    assert _consumed(Session) == [False]


def test_headerless_message_uses_the_old_window_while_beat_has_never_stamped(
    env, caplog
):
    """Rolling upgrade: an old beat sends no headers. Until any stamped run
    has been seen, header-less messages keep the wall-clock window."""
    _app, Session, echo, calls = env
    _add_override(Session, datetime.now(timezone.utc))

    with caplog.at_level(logging.WARNING, logger="celery_manager.decorator"):
        echo.apply(args=["legacy"])

    assert calls == []
    assert _consumed(Session) == [True]
    assert "no occurrence headers" in caplog.text


def test_stamped_message_for_another_occurrence_does_not_match(env):
    _app, Session, echo, calls = env
    target = datetime.now(timezone.utc)
    _add_override(Session, target + EVERY)  # the *next* firing is skipped

    echo.apply(
        args=["this one"], headers=_stamp(target, interval=EVERY)
    )  # starts right on time
    assert calls == ["this one"]
    assert _consumed(Session) == [False]


def test_calendar_occurrences_match_exactly_not_by_tolerance(env):
    _app, Session, echo, calls = env
    target = _utc(2026, 8, 15, 10, 0)
    _add_override(Session, target)

    # An every-minute crontab: 10:01 is a different occurrence, though within 60 s.
    echo.apply(args=["10:01"], headers=_stamp(target + timedelta(minutes=1)))
    assert calls == ["10:01"]
    assert _consumed(Session) == [False]

    # Millisecond rounding from the browser still matches.
    echo.apply(args=["10:00"], headers=_stamp(target + timedelta(microseconds=400)))
    assert calls == ["10:01"]
    assert _consumed(Session) == [True]


def test_interval_occurrences_match_within_tolerance_capped_at_half_the_period(env):
    _app, Session, echo, calls = env
    target = _utc(2026, 8, 15, 10, 0)
    _add_override(Session, target)

    # Period 60 s: window is min(60, 30) = 30 s, so 40 s off is another firing.
    echo.apply(
        args=["neighbour"],
        headers=_stamp(target + timedelta(seconds=40), interval=timedelta(seconds=60)),
    )
    assert calls == ["neighbour"]

    # Period 2 h: beat jitter of a few seconds is the same occurrence.
    echo.apply(
        args=["jittered"], headers=_stamp(target + timedelta(seconds=3), interval=EVERY)
    )
    assert calls == ["neighbour"]
    assert _consumed(Session) == [True]


def test_redelivered_or_retried_occurrence_does_not_rematch_a_consumed_override(env):
    _app, Session, echo, calls = env
    target = datetime.now(timezone.utc) - timedelta(minutes=5)
    _add_override(Session, target)

    echo.apply(args=["first"], headers=_stamp(target, interval=EVERY))
    echo.apply(args=["again"], headers=_stamp(target, interval=EVERY))

    assert calls == ["again"]
    assert _consumed(Session) == [True]


def test_retrying_a_stamped_run_keeps_its_occurrence_and_records_retried(env):
    app, Session, _echo, _calls = env
    attempts = []

    @app.task(name="demo.flaky", bind=True)
    @track(schedule_name=NAME)
    def flaky(self):
        attempts.append(1)
        raise Retry("try again")

    occurrence = _utc(2026, 8, 15, 10, 0)
    flaky.apply(task_id="flaky-1", headers=_stamp(occurrence, interval=EVERY))
    with Session() as s:
        run = s.query(TaskRun).filter_by(task_id="flaky-1").one()
        assert run.status == "retried"
        assert run.occurrence_at.replace(tzinfo=timezone.utc) == occurrence

    # A retry attempt published by Celery carries no custom headers: it must
    # run (not consume anything) and must not erase the stamp.
    _add_override(Session, datetime.now(timezone.utc))
    flaky.apply(task_id="flaky-1")
    assert len(attempts) == 2
    assert _consumed(Session) == [False]
    with Session() as s:
        run = s.query(TaskRun).filter_by(task_id="flaky-1").one()
        assert run.occurrence_at.replace(tzinfo=timezone.utc) == occurrence


def test_whole_schedule_pause_still_skips_stamped_and_manual_runs(env):
    _app, Session, echo, calls = env
    with Session() as s:
        pause_schedule(s, NAME, "alice")

    echo.apply(
        args=["beat"], headers=_stamp(datetime.now(timezone.utc), interval=EVERY)
    )
    echo.apply(args=["manual"])
    assert calls == []


def test_stamped_entry_name_is_used_for_overrides(env):
    """The header names the beat entry, so a task whose `@track` name differs
    (or is the task name) still gets the skip saved for that entry."""
    app, Session, _echo, _calls = env
    ran = []

    @app.task(name="demo.other", bind=True)
    @track()
    def other(self):
        ran.append(1)

    target = datetime.now(timezone.utc)
    _add_override(Session, target)
    other.apply(headers=_stamp(target, interval=EVERY))
    assert ran == []
    assert _consumed(Session) == [True]


def test_occurrence_and_sent_at_are_recorded_on_the_run(env):
    _app, Session, echo, _calls = env
    occurrence = _utc(2026, 8, 15, 10, 0)
    sent_at = occurrence + timedelta(milliseconds=12)
    echo.apply(
        args=["x"],
        task_id="t1",
        headers=_stamp(occurrence, interval=EVERY, sent_at=sent_at),
    )
    with Session() as s:
        run = s.query(TaskRun).filter_by(task_id="t1").one()
        assert run.occurrence_at.replace(tzinfo=timezone.utc) == occurrence
        assert run.beat_sent_at.replace(tzinfo=timezone.utc) == sent_at


def test_headers_are_read_from_request_attributes_as_a_real_worker_sets_them():
    occurrence = _utc(2026, 8, 15, 10, 0)
    task_self = SimpleNamespace(
        request=SimpleNamespace(
            cm_schedule=NAME,
            cm_occurrence=occurrence.isoformat(),
            cm_interval="7200",
            headers=None,
        )
    )
    found = decorator_module._beat_occurrence(task_self)
    assert found.schedule_name == NAME
    assert found.occurrence == occurrence
    assert found.interval_seconds == 7200


@pytest.mark.parametrize(
    "headers",
    [
        {"cm_schedule": NAME},
        {"cm_schedule": NAME, "cm_occurrence": "not a time"},
        {"cm_schedule": "", "cm_occurrence": "2026-08-15T10:00:00+00:00"},
        {"cm_schedule": NAME, "cm_occurrence": 12},
    ],
)
def test_malformed_headers_count_as_no_headers(headers):
    task_self = SimpleNamespace(request=SimpleNamespace(headers=headers))
    assert decorator_module._beat_occurrence(task_self) is None


# --- beat side -----------------------------------------------------------------


class _InMemoryScheduler(StampOccurrencesMixin, CeleryScheduler):
    """The mixin on Celery's base (in-memory) scheduler; the bundled
    `Scheduler` puts it on `PersistentScheduler`, which needs a shelve file."""


def _beat(app) -> CeleryScheduler:
    return _InMemoryScheduler(app=app, lazy=True)


def test_bundled_scheduler_is_the_persistent_scheduler_with_the_mixin():
    from celery.beat import PersistentScheduler

    assert issubclass(Scheduler, StampOccurrencesMixin)
    assert issubclass(Scheduler, PersistentScheduler)


def _entry(app, schedule, last_run_at, name=NAME):
    return ScheduleEntry(
        name=name, task="demo.echo", last_run_at=last_run_at, schedule=schedule, app=app
    )


def _sent_headers(app, scheduler, entry, advance=False) -> dict:
    task = app.tasks["demo.echo"]
    with patch.object(task, "apply_async") as send:
        scheduler.apply_async(entry, producer=None, advance=advance)
    return send.call_args.kwargs["headers"]


def test_crontab_stamp_equals_the_occurrence_the_dashboard_lists(env):
    app, Session, echo, calls = env
    every_2h = crontab(minute=0, hour="*/2")
    app.conf.beat_schedule = {NAME: {"task": "demo.echo", "schedule": every_2h}}

    with Session() as s:
        shown = list_upcoming_with_overrides(
            s, app, NAME, count=2, from_time=_utc(2026, 8, 15, 5, 30)
        )
        assert shown[0]["target_time"] == _utc(2026, 8, 15, 6, 0)
        pause_occurrence(
            s, NAME, shown[0]["target_time"], action="skip", created_by="alice"
        )

    # Beat fires 06:00 (+50 ms); its previous firing was at 04:00 (+200 ms).
    fire_at = _utc(2026, 8, 15, 6, 0, 0, 50_000)
    schedule = crontab(minute=0, hour="*/2", nowfun=lambda: fire_at)
    headers = _sent_headers(
        app, _beat(app), _entry(app, schedule, _utc(2026, 8, 15, 4, 0, 0, 200_000))
    )
    assert headers["cm_schedule"] == NAME
    assert datetime.fromisoformat(headers["cm_occurrence"]) == shown[0]["target_time"]
    assert "cm_interval" not in headers

    # Delivered hours late, it is still that occurrence and is skipped.
    echo.apply(args=["late"], headers=headers)
    assert calls == []
    assert _consumed(Session) == [True]


def test_interval_stamp_equals_the_occurrence_the_dashboard_lists(env):
    app, Session, echo, calls = env
    first_sent = _utc(2026, 8, 15, 6, 0, 0, 300_000)

    # First firing, stamped and recorded (its start was 4 minutes late).
    first = _stamp(first_sent, interval=EVERY, sent_at=first_sent)
    with patch("celery_manager.decorator.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: first_sent + timedelta(minutes=4)
        echo.apply(args=["first"], headers=first)

    # The list anchors on beat's send time, not on the late start.
    with Session() as s:
        shown = list_upcoming_with_overrides(
            s, app, NAME, count=2, from_time=first_sent + timedelta(minutes=30)
        )
        assert shown[0]["target_time"] == first_sent + EVERY
        pause_occurrence(
            s, NAME, shown[0]["target_time"], action="skip", created_by="alice"
        )

    fire_at = first_sent + EVERY + timedelta(milliseconds=7)
    schedule = interval_schedule(run_every=EVERY, nowfun=lambda: fire_at)
    headers = _sent_headers(app, _beat(app), _entry(app, schedule, first_sent))
    assert datetime.fromisoformat(headers["cm_occurrence"]) == shown[0]["target_time"]
    assert headers["cm_interval"] == EVERY.total_seconds()

    echo.apply(args=["second"], headers=headers)
    assert calls == ["first"]
    assert _consumed(Session) == [True]


def test_advance_path_stamps_and_stores_the_entry_unstamped(env):
    app, _Session, _echo, _calls = env
    scheduler = _beat(app)
    last = _utc(2026, 8, 15, 6, 0)
    fire_at = last + EVERY
    entry = _entry(
        app, interval_schedule(run_every=EVERY, nowfun=lambda: fire_at), last
    )

    headers = _sent_headers(app, scheduler, entry, advance=True)

    assert datetime.fromisoformat(headers["cm_occurrence"]) == last + EVERY
    stored = scheduler.schedule[NAME]
    assert stored.last_run_at != last  # advanced
    assert "headers" not in stored.options


def test_existing_option_headers_are_kept(env):
    app, _Session, _echo, _calls = env
    last = _utc(2026, 8, 15, 6, 0)
    entry = ScheduleEntry(
        name=NAME,
        task="demo.echo",
        last_run_at=last,
        app=app,
        schedule=interval_schedule(run_every=EVERY, nowfun=lambda: last + EVERY),
        options={"headers": {"x-tenant": "acme"}},
    )
    headers = _sent_headers(app, _beat(app), entry)
    assert headers["x-tenant"] == "acme"
    assert "cm_occurrence" in headers
    assert entry.options == {"headers": {"x-tenant": "acme"}}


def test_a_stamping_failure_still_sends_the_task(env):
    app, _Session, _echo, _calls = env
    entry = _entry(app, interval_schedule(run_every=EVERY), _utc(2026, 8, 15, 6, 0))
    task = app.tasks["demo.echo"]
    with (
        patch(
            "celery_manager.beat.occurrence_headers", side_effect=RuntimeError("boom")
        ),
        patch.object(task, "apply_async") as send,
    ):
        _beat(app).apply_async(entry, producer=None, advance=False)
    assert send.called
    assert "headers" not in send.call_args.kwargs


def test_mixin_works_in_front_of_any_scheduler_class(env):
    app, _Session, _echo, _calls = env

    class Base(CeleryScheduler):
        pass

    class Custom(StampOccurrencesMixin, Base):
        pass

    last = _utc(2026, 8, 15, 6, 0)
    entry = _entry(
        app, interval_schedule(run_every=EVERY, nowfun=lambda: last + EVERY), last
    )
    headers = _sent_headers(app, Custom(app=app, lazy=True), entry)
    assert headers["cm_schedule"] == NAME


def test_due_occurrence_after_beat_downtime_is_the_latest_due_point():
    last = _utc(2026, 8, 15, 6, 0)
    interval = interval_schedule(run_every=EVERY)
    assert (
        due_occurrence(interval, last, last + 5 * EVERY + timedelta(seconds=10))
        == last + 5 * EVERY
    )

    hourly = crontab(minute=0)
    assert due_occurrence(hourly, last, _utc(2026, 8, 15, 11, 20)) == _utc(
        2026, 8, 15, 11, 0
    )


def test_occurrence_headers_uses_the_pre_advance_last_run_at():
    entry = MagicMock()
    entry.name = NAME
    last = _utc(2026, 8, 15, 6, 0)
    entry.last_run_at = last
    entry.schedule = interval_schedule(run_every=EVERY, nowfun=lambda: last + EVERY)
    headers = occurrence_headers(
        entry, sent_at=last + EVERY + timedelta(milliseconds=3)
    )
    assert datetime.fromisoformat(headers["cm_occurrence"]) == last + EVERY
    assert datetime.fromisoformat(headers["cm_sent_at"]) == last + EVERY + timedelta(
        milliseconds=3
    )
