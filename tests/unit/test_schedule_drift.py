# tests/unit/test_schedule_drift.py
"""Interval-schedule occurrence drift.

Found against a real beat + worker: for a `schedule(run_every=...)` entry,
upcoming occurrences were computed as `now + k * run_every`, so they moved
forward on every request. A skip saved for "the next occurrence" stopped
matching the displayed list once more than `eta_tolerance_seconds` had
passed (display), and the saved `target_time` could be up to a whole
interval away from when beat really fired, so the decorator's run-time
check missed it (enforcement). Occurrences are now projected from the
schedule's last observed run (`task_runs.started_at`), which is what beat
itself does (`last_run_at + run_every`).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery
from celery.schedules import crontab
from celery.schedules import schedule as interval_schedule
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.schedules import (
    list_upcoming_with_overrides,
    pause_occurrence,
    sync_periodic_schedules,
    upcoming_occurrences,
)
from celery_manager.db.models import Base, PeriodicSchedule, ScheduleOverride, TaskRun
from celery_manager.decorator import configure, track

NAME = "demo-every-2h"
EVERY = timedelta(hours=2)
LAST_RUN = datetime(2026, 8, 15, 6, 0, 0, 300000, tzinfo=timezone.utc)


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _app(schedule=None):
    app = MagicMock(spec=Celery)
    app.conf.beat_schedule = {NAME: {"task": "demo.echo", "schedule": schedule or interval_schedule(run_every=EVERY)}}
    return app


def _record_run(session, started_at, **extra):
    session.add(TaskRun(
        task_id=f"run-{started_at.isoformat()}", task_name="demo.echo", status="succeeded",
        schedule_name=NAME, started_at=started_at, **extra,
    ))
    session.commit()


# --- control layer ---------------------------------------------------------


def test_interval_occurrences_are_stable_across_reads_once_a_run_is_observed():
    Session = _session_factory()
    app = _app()
    with Session() as s:
        sync_periodic_schedules(s, app)
        _record_run(s, LAST_RUN)

    # Two reads 7 minutes apart — far beyond the 60s tolerance, which is
    # exactly what used to make the list drift out from under a saved skip.
    with Session() as s:
        first = list_upcoming_with_overrides(s, app, NAME, count=3, from_time=LAST_RUN + timedelta(minutes=3))
    with Session() as s:
        second = list_upcoming_with_overrides(s, app, NAME, count=3, from_time=LAST_RUN + timedelta(minutes=10))

    expected = [LAST_RUN + EVERY, LAST_RUN + 2 * EVERY, LAST_RUN + 3 * EVERY]
    assert [r["target_time"] for r in first] == expected
    assert [r["target_time"] for r in second] == expected


def test_saved_skip_still_matches_the_same_occurrence_after_time_passes():
    Session = _session_factory()
    app = _app()
    with Session() as s:
        sync_periodic_schedules(s, app)
        _record_run(s, LAST_RUN)

    with Session() as s:
        shown = list_upcoming_with_overrides(s, app, NAME, count=3, from_time=LAST_RUN + timedelta(minutes=1))
    with Session() as s:
        pause_occurrence(s, NAME, shown[0]["target_time"], action="skip", created_by="alice")

    # 45 minutes later (a page reload long after the pause).
    with Session() as s:
        later = list_upcoming_with_overrides(s, app, NAME, count=3, from_time=LAST_RUN + timedelta(minutes=46))

    assert later[0]["target_time"] == shown[0]["target_time"]
    assert later[0]["action"] == "skip"
    assert [r["action"] for r in later[1:]] == [None, None]


def test_newest_observed_run_is_the_anchor_and_reanchoring_keeps_the_match():
    Session = _session_factory()
    app = _app()
    with Session() as s:
        sync_periodic_schedules(s, app)
        _record_run(s, LAST_RUN - EVERY)
        _record_run(s, LAST_RUN)
        pause_occurrence(s, NAME, LAST_RUN + 2 * EVERY, action="cancel", created_by="alice")
        # Beat's next fire lands with a little queue latency; the new run
        # re-anchors the grid by that latency, which tolerance absorbs.
        _record_run(s, LAST_RUN + EVERY + timedelta(seconds=2))

    with Session() as s:
        result = list_upcoming_with_overrides(s, app, NAME, count=2, from_time=LAST_RUN + EVERY + timedelta(minutes=1))

    assert result[0]["target_time"] == LAST_RUN + 2 * EVERY + timedelta(seconds=2)
    assert result[0]["action"] == "cancel"
    assert result[1]["action"] is None


def test_dashboard_retries_do_not_reanchor_the_schedule():
    Session = _session_factory()
    app = _app()
    with Session() as s:
        sync_periodic_schedules(s, app)
        _record_run(s, LAST_RUN)
        _record_run(s, LAST_RUN + timedelta(minutes=17), retried_from_id="run-original")

    with Session() as s:
        result = list_upcoming_with_overrides(s, app, NAME, count=1, from_time=LAST_RUN + timedelta(minutes=20))

    assert result[0]["target_time"] == LAST_RUN + EVERY


def test_runs_of_other_schedules_are_not_used_as_the_anchor():
    Session = _session_factory()
    app = _app()
    with Session() as s:
        sync_periodic_schedules(s, app)
        _record_run(s, LAST_RUN)
        s.add(TaskRun(task_id="other", task_name="demo.echo", status="succeeded",
                      schedule_name="something-else", started_at=LAST_RUN + timedelta(minutes=5)))
        s.commit()

    with Session() as s:
        result = list_upcoming_with_overrides(s, app, NAME, count=1, from_time=LAST_RUN + timedelta(minutes=20))

    assert result[0]["target_time"] == LAST_RUN + EVERY


def test_falls_back_to_a_from_now_estimate_before_any_run_is_observed():
    """Documented residual limitation: with no observed run there is nothing
    to anchor to, so the projection is an estimate relative to "now"."""
    Session = _session_factory()
    app = _app()
    with Session() as s:
        sync_periodic_schedules(s, app)
        start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
        result = list_upcoming_with_overrides(s, app, NAME, count=1, from_time=start)

    assert result[0]["target_time"] == start + EVERY


def test_anchor_projection_handles_from_time_far_past_the_anchor():
    sched = PeriodicSchedule(name=NAME, task_name="demo.echo", schedule_spec="2h")
    # Beat was down for a day: the grid still projects deterministically.
    result = upcoming_occurrences(
        sched, interval_schedule(run_every=EVERY), count=2,
        from_time=LAST_RUN + timedelta(days=1, minutes=30), anchor=LAST_RUN,
    )
    assert result == [LAST_RUN + 13 * EVERY, LAST_RUN + 14 * EVERY]


def test_crontab_ignores_the_anchor():
    sched = PeriodicSchedule(name="daily", task_name="demo.echo", schedule_spec="crontab")
    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
    result = upcoming_occurrences(sched, crontab(hour=7, minute=30), count=1, from_time=start, anchor=LAST_RUN)
    assert result == [datetime(2026, 8, 15, 7, 30, tzinfo=timezone.utc)]


# --- REST layer: real "now" advancing between requests ----------------------


def test_api_skip_still_shows_as_skipped_after_the_clock_moves_past_tolerance():
    celery_app = Celery("test")
    celery_app.conf.beat_schedule = {NAME: {"task": "demo.echo", "schedule": interval_schedule(run_every=EVERY)}}
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as s:
        create_user(s, "alice", "pw")
        _record_run(s, LAST_RUN)
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})

    clock = {"now": LAST_RUN + timedelta(minutes=2)}
    with patch("celery_manager.control.schedules.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: clock["now"]
        shown = client.get(f"/api/schedules/{NAME}/occurrences").json()
        assert client.post(
            f"/api/schedules/{NAME}/occurrences/pause",
            json={"target_time": shown[0]["target_time"], "action": "skip"},
        ).status_code == 200

        clock["now"] = LAST_RUN + timedelta(minutes=30)
        refreshed = client.get(f"/api/schedules/{NAME}/occurrences").json()

    assert refreshed[0]["target_time"] == shown[0]["target_time"]
    assert refreshed[0]["action"] == "skip"
    assert all(r["action"] is None for r in refreshed[1:])


# --- enforcement: the decorator skips the real beat fire --------------------


@pytest.fixture
def worker():
    app = Celery("drift_app", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    Session = _session_factory()
    configure(session_factory=Session, eta_tolerance_seconds=60)
    calls = []

    @app.task(name="demo.echo", bind=True)
    @track(schedule_name=NAME)
    def echo(self, value):
        calls.append(value)
        return value

    return Session, echo, calls


def _run_at(task, when, value):
    with patch("celery_manager.decorator.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: when
        task.apply(args=[value])


def test_skipping_the_displayed_occurrence_skips_the_real_beat_fire(worker):
    Session, echo, calls = worker
    beat_app = _app()
    with Session() as s:
        sync_periodic_schedules(s, beat_app)

    # Beat fires the schedule; the worker records the run.
    _run_at(echo, LAST_RUN, "first")

    # Operator opens the dashboard 37 minutes later and skips the next one.
    # (Pre-fix this displayed and saved 06:37 + 2h = 08:37, 37 min after
    # the real fire, so the run-time check below could never match it.)
    with Session() as s:
        shown = list_upcoming_with_overrides(s, beat_app, NAME, count=2, from_time=LAST_RUN + timedelta(minutes=37))
        assert shown[0]["target_time"] == LAST_RUN + EVERY
        pause_occurrence(s, NAME, shown[0]["target_time"], action="skip", created_by="alice")

    # Beat fires on its own grid (+ a little queue latency).
    _run_at(echo, LAST_RUN + EVERY + timedelta(milliseconds=800), "second")
    # And the one after runs normally.
    _run_at(echo, LAST_RUN + 2 * EVERY + timedelta(milliseconds=500), "third")

    assert calls == ["first", "third"]
    with Session() as s:
        override = s.query(ScheduleOverride).filter_by(schedule_name=NAME).one()
        assert override.consumed is True
        statuses = [r.status for r in s.query(TaskRun).order_by(TaskRun.started_at)]
        assert statuses == ["succeeded", "skipped", "succeeded"]

        # The skipped fire itself re-anchors the grid, so the list moves on
        # cleanly with no stale "skip" left behind.
        after = list_upcoming_with_overrides(s, beat_app, NAME, count=2, from_time=LAST_RUN + 2 * EVERY + timedelta(minutes=1))
        assert all(r["action"] is None for r in after)
        assert abs(after[0]["target_time"] - (LAST_RUN + 3 * EVERY)) < timedelta(seconds=1)
