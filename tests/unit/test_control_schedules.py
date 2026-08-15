# tests/unit/test_control_schedules.py
from datetime import datetime, timedelta, timezone

from celery.schedules import crontab, schedule as interval_schedule
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, PeriodicSchedule, ScheduleOverride
from celery_manager.control.schedules import (
    upcoming_occurrences,
    pause_occurrence,
    list_overrides,
    sync_periodic_schedules,
    list_upcoming_with_overrides,
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
