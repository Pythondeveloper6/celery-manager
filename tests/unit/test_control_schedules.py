# tests/unit/test_control_schedules.py
from datetime import datetime, timedelta, timezone

from celery.schedules import crontab, schedule as interval_schedule
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, PeriodicSchedule
from celery_manager.control.schedules import (
    upcoming_occurrences,
    pause_occurrence,
    list_overrides,
    sync_periodic_schedules,
    list_upcoming_with_overrides,
    list_periodic_schedules,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_upcoming_occurrences_for_interval_schedule():
    sched = PeriodicSchedule(name="demo-every-2h", task_name="demo.echo", schedule_spec="2h")
    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)

    occurrences = upcoming_occurrences(sched, interval_schedule(run_every=timedelta(hours=2)), count=3, from_time=start)

    assert occurrences == [
        datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
    ]


def test_upcoming_occurrences_for_crontab_schedule():
    sched = PeriodicSchedule(name="daily-report", task_name="demo.echo", schedule_spec="crontab(hour=7,minute=30)")
    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)

    occurrences = upcoming_occurrences(sched, crontab(hour=7, minute=30), count=2, from_time=start)

    assert occurrences == [
        datetime(2026, 8, 15, 7, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 16, 7, 30, tzinfo=timezone.utc),
    ]


def test_pause_occurrence_creates_override():
    session = _session()
    session.add(PeriodicSchedule(name="demo-every-2h", task_name="demo.echo", schedule_spec="2h"))
    session.commit()

    target = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    override = pause_occurrence(session, "demo-every-2h", target, action="skip", created_by="alice")

    assert override.id is not None
    assert list_overrides(session, "demo-every-2h") == [override]


def test_pause_occurrence_is_idempotent_for_same_target():
    session = _session()
    session.add(PeriodicSchedule(name="demo-every-2h", task_name="demo.echo", schedule_spec="2h"))
    session.commit()

    target = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    pause_occurrence(session, "demo-every-2h", target, action="skip", created_by="alice")
    pause_occurrence(session, "demo-every-2h", target, action="skip", created_by="alice")

    assert len(list_overrides(session, "demo-every-2h")) == 1


def test_sync_periodic_schedules_upserts_from_beat_schedule():
    from unittest.mock import MagicMock
    from celery import Celery

    session = _session()
    app = MagicMock(spec=Celery)
    app.conf.beat_schedule = {
        "demo-every-2h": {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=2)), "args": [], "kwargs": {}},
    }

    synced = sync_periodic_schedules(session, app)

    assert len(synced) == 1
    assert synced[0].name == "demo-every-2h"
    assert synced[0].task_name == "demo.echo"

    # calling again does not duplicate the row
    sync_periodic_schedules(session, app)
    assert session.query(PeriodicSchedule).count() == 1


def test_list_upcoming_with_overrides_merges_computed_occurrences_and_overrides():
    from unittest.mock import MagicMock
    from celery import Celery

    # Two independent Session objects on the same engine — mirrors two
    # separate HTTP requests each opening their own session via
    # session_factory() (Task 10's routes). This is deliberate: it's what
    # actually exercises SQLite's tzinfo-dropping round-trip (a session
    # that only ever reads back objects it inserted itself, via SQLAlchemy's
    # identity map, never re-parses the stored value and would hide the bug
    # _as_aware_utc fixes).
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    app = MagicMock(spec=Celery)
    app.conf.beat_schedule = {
        "demo-every-2h": {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=2)), "args": [], "kwargs": {}},
    }
    with Session() as write_session:
        sync_periodic_schedules(write_session, app)
        first_occurrence = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)
        pause_occurrence(write_session, "demo-every-2h", first_occurrence, action="skip", created_by="alice")

    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
    read_session = Session()
    result = list_upcoming_with_overrides(read_session, app, "demo-every-2h", count=3, from_time=start)

    assert len(result) == 3
    assert result[0] == {"target_time": first_occurrence, "action": "skip", "consumed": False}
    assert result[1]["action"] is None
    assert result[1]["consumed"] is False


def test_list_upcoming_with_overrides_matches_within_tolerance_despite_time_drift():
    from unittest.mock import MagicMock
    from celery import Celery

    session = _session()
    app = MagicMock(spec=Celery)
    app.conf.beat_schedule = {
        "demo-every-2h": {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=2)), "args": [], "kwargs": {}},
    }
    sync_periodic_schedules(session, app)

    # Simulates the realistic gap this fix is for: a user pauses an
    # occurrence a few seconds after it was first computed/displayed, so
    # by the time upcoming occurrences are recomputed relative to a fresh
    # `from_time`, the two timestamps for "the same" occurrence are close
    # but not byte-identical.
    computed_occurrence = datetime(2026, 8, 15, 8, 0, 0, tzinfo=timezone.utc)
    paused_at = computed_occurrence + timedelta(seconds=30)
    pause_occurrence(session, "demo-every-2h", paused_at, action="skip", created_by="alice")

    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
    result = list_upcoming_with_overrides(session, app, "demo-every-2h", count=3, from_time=start, eta_tolerance_seconds=60)

    assert result[0]["target_time"] == computed_occurrence
    assert result[0]["action"] == "skip"
    assert result[0]["consumed"] is False


def test_list_upcoming_with_overrides_does_not_match_outside_tolerance():
    from unittest.mock import MagicMock
    from celery import Celery

    session = _session()
    app = MagicMock(spec=Celery)
    app.conf.beat_schedule = {
        "demo-every-2h": {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=2)), "args": [], "kwargs": {}},
    }
    sync_periodic_schedules(session, app)

    # 5 minutes away from the nearest computed occurrence (08:00) — well
    # outside the default 60s tolerance. Confirms tolerance-window
    # matching doesn't degrade into "match anything nearby" and silently
    # show an unrelated pause against a schedule occurrence it was never
    # meant for.
    far_target = datetime(2026, 8, 15, 8, 5, 0, tzinfo=timezone.utc)
    pause_occurrence(session, "demo-every-2h", far_target, action="skip", created_by="alice")

    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
    result = list_upcoming_with_overrides(session, app, "demo-every-2h", count=3, from_time=start, eta_tolerance_seconds=60)

    assert all(r["action"] is None for r in result)
    assert all(r["consumed"] is False for r in result)


def test_pause_occurrence_converges_repeated_calls_with_drifted_target_time_to_one_row():
    session = _session()
    session.add(PeriodicSchedule(name="demo-every-2h", task_name="demo.echo", schedule_spec="2h"))
    session.commit()

    # Simulates a user re-deciding on "the same" occurrence after a page
    # reload: the dashboard recomputed occurrence timestamps in between,
    # so the second pause request's target_time has drifted a few seconds
    # from the first — but it should still be recognized as the same
    # occurrence and update the one row, not create a second.
    first_target = datetime(2026, 8, 15, 20, 0, 0, tzinfo=timezone.utc)
    second_target = first_target + timedelta(seconds=5)

    pause_occurrence(session, "demo-every-2h", first_target, action="skip", created_by="alice")
    # Assert against the second call's own return value (exactly how the
    # real route uses pause_occurrence's result — see api/routes/schedules.py)
    # rather than re-querying via list_overrides: an unreferenced ORM
    # instance is eligible for GC as soon as nothing holds it, and a
    # subsequent query that has to re-fetch from SQLite hits the same
    # well-known tzinfo-dropping round-trip _as_aware_utc exists for —
    # a pre-existing, unrelated quirk of this same-session, not something
    # this test is about.
    updated = pause_occurrence(session, "demo-every-2h", second_target, action="cancel", created_by="alice")

    # The most recent request wins: both the action and the target_time
    # are updated on the existing row, rather than the first request's
    # values sticking around alongside (or instead of) the second's.
    assert updated.action == "cancel"
    assert updated.target_time == second_target
    assert len(list_overrides(session, "demo-every-2h")) == 1


def test_list_upcoming_with_overrides_does_not_double_match_closely_spaced_occurrences():
    from unittest.mock import MagicMock
    from celery import Celery

    session = _session()
    app = MagicMock(spec=Celery)
    app.conf.beat_schedule = {
        "demo-every-90s": {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(seconds=90)), "args": [], "kwargs": {}},
    }
    sync_periodic_schedules(session, app)

    start = datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc)
    # Occurrences land at 06:01:30, 06:03:00, 06:04:30 — only 90s apart,
    # well under 2x the default 60s tolerance. A single override sitting
    # at the midpoint between the first two (45s from each) falls within
    # tolerance of BOTH — without claim/dedup this would show as two
    # paused rows for one actual pause.
    near_boundary = start + timedelta(seconds=90 + 45)  # 06:02:15
    pause_occurrence(session, "demo-every-90s", near_boundary, action="skip", created_by="alice")

    result = list_upcoming_with_overrides(session, app, "demo-every-90s", count=3, from_time=start, eta_tolerance_seconds=60)

    matched = [r for r in result if r["action"] == "skip"]
    assert len(matched) == 1
    # The earlier (chronologically first) occurrence claims the override.
    assert matched[0]["target_time"] == start + timedelta(seconds=90)


def test_list_periodic_schedules_returns_all_ordered_by_name():
    session = _session()
    session.add(PeriodicSchedule(name="zzz-last", task_name="demo.echo", schedule_spec="1h"))
    session.add(PeriodicSchedule(name="aaa-first", task_name="demo.echo", schedule_spec="2h"))
    session.commit()

    result = list_periodic_schedules(session)

    assert [s.name for s in result] == ["aaa-first", "zzz-last"]


def test_list_periodic_schedules_empty_when_none_configured():
    session = _session()
    assert list_periodic_schedules(session) == []


# --- I3: bare-number / timedelta beat_schedule entries ---
#
# Idiomatic Celery config allows `"schedule": 30.0` or
# `"schedule": timedelta(hours=2)` — beat normalizes those internally via
# maybe_schedule(). celery-manager read entry["schedule"] raw, so anything
# that wasn't already a schedule object crashed with
# `AttributeError: 'float' object has no attribute 'nowfun'`, i.e. a 500 from
# GET /api/schedules/{name}/occurrences.


def _app_with_bare_schedules():
    from unittest.mock import MagicMock
    from celery import Celery

    app = MagicMock(spec=Celery)
    app.conf.beat_schedule = {
        "bare-number": {"task": "demo.echo", "schedule": 30.0},
        "bare-timedelta": {"task": "demo.echo", "schedule": timedelta(hours=2)},
    }
    return app


def test_sync_periodic_schedules_accepts_bare_number_and_timedelta():
    session = _session()
    app = _app_with_bare_schedules()

    synced = sync_periodic_schedules(session, app)

    assert {s.name for s in synced} == {"bare-number", "bare-timedelta"}
    # Normalized for display too, rather than storing a bare "30.0".
    specs = {s.name: s.schedule_spec for s in synced}
    assert "30.00 seconds" in specs["bare-number"]
    assert "2.00 hours" in specs["bare-timedelta"]


def test_upcoming_occurrences_does_not_crash_on_a_bare_number_schedule():
    session = _session()
    app = _app_with_bare_schedules()
    sync_periodic_schedules(session, app)
    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)

    result = list_upcoming_with_overrides(session, app, "bare-number", count=3, from_time=start)

    assert [r["target_time"] for r in result] == [
        datetime(2026, 8, 15, 6, 0, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 15, 6, 1, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 15, 6, 1, 30, tzinfo=timezone.utc),
    ]


def test_upcoming_occurrences_does_not_crash_on_a_bare_timedelta_schedule():
    session = _session()
    app = _app_with_bare_schedules()
    sync_periodic_schedules(session, app)
    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)

    result = list_upcoming_with_overrides(session, app, "bare-timedelta", count=2, from_time=start)

    assert [r["target_time"] for r in result] == [
        datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
    ]
