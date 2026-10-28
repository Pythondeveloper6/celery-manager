"""Missed-run detection (control/missed.py) and its REST surface."""

from datetime import datetime, timedelta, timezone

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
from celery_manager.control.missed import list_missed_runs, missed_run_counts
from celery_manager.db.models import Base, TaskRun

HOURLY = "hourly"
EVERY_10M = "every-10m"
NOW = datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)
STEP = timedelta(minutes=10)


def _app():
    app = Celery("t")
    app.conf.beat_schedule = {
        HOURLY: {"task": "demo.echo", "schedule": crontab(minute=0)},
        EVERY_10M: {"task": "demo.echo", "schedule": interval_schedule(run_every=STEP)},
    }
    return app


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def _run(session, name, occurrence, status="succeeded", sent=None):
    session.add(TaskRun(
        task_id=f"{name}-{occurrence.isoformat()}-{status}", task_name="demo.echo", status=status,
        schedule_name=name, started_at=occurrence, occurrence_at=occurrence,
        beat_sent_at=sent if sent is not None else occurrence,
    ))
    session.commit()


def _hour(h):
    return datetime(2026, 10, 28, h, 0, tzinfo=timezone.utc)


def _due(result):
    return [r["due_at"] for r in result]


def test_crontab_missed_slot_is_detected(session):
    _run(session, HOURLY, _hour(8))
    _run(session, HOURLY, _hour(9))
    _run(session, HOURLY, _hour(11))  # 10:00 never arrived
    _run(session, HOURLY, _hour(12))
    assert _due(list_missed_runs(session, _app(), HOURLY, now=NOW)) == [_hour(10)]


@pytest.mark.parametrize("status", ["skipped", "revoked", "failed"])
def test_runs_that_reached_the_worker_count_as_arrived(session, status):
    _run(session, HOURLY, _hour(8))
    _run(session, HOURLY, _hour(9), status=status)
    _run(session, HOURLY, _hour(10), status="skipped")  # e.g. whole schedule paused
    _run(session, HOURLY, _hour(11))
    _run(session, HOURLY, _hour(12))
    assert list_missed_runs(session, _app(), HOURLY, now=NOW) == []


def test_nothing_is_judged_before_the_first_stamp(session):
    # Stamping started at 10:00; the empty hours before it are unknowable.
    _run(session, HOURLY, _hour(10))
    _run(session, HOURLY, _hour(11))
    _run(session, HOURLY, _hour(12))
    assert list_missed_runs(session, _app(), HOURLY, now=NOW, since=_hour(0)) == []


def test_unstamped_schedule_is_never_reported(session):
    session.add(TaskRun(task_id="x", task_name="demo.echo", status="succeeded", schedule_name=HOURLY,
                        started_at=_hour(9)))
    session.commit()
    assert list_missed_runs(session, _app(), HOURLY, now=NOW) == []
    assert missed_run_counts(session, _app(), NOW - timedelta(days=1), now=NOW) == {}


def test_unknown_schedule_yields_nothing(session):
    assert list_missed_runs(session, _app(), "nope", now=NOW) == []


def test_grace_delays_reporting(session):
    _run(session, HOURLY, _hour(10))
    # 12:00 is due; at 12:03 it is still inside the 5 minute grace.
    soon = _hour(12) + timedelta(minutes=3)
    assert _due(list_missed_runs(session, _app(), HOURLY, now=soon)) == [_hour(11)]
    later = _hour(12) + timedelta(minutes=6)
    assert _due(list_missed_runs(session, _app(), HOURLY, now=later)) == [_hour(11), _hour(12)]
    # A larger grace holds 12:00 back again.
    assert _due(list_missed_runs(session, _app(), HOURLY, now=later, grace_seconds=900)) == [_hour(11)]


def test_since_and_until_bound_the_window(session):
    _run(session, HOURLY, _hour(6))
    got = list_missed_runs(session, _app(), HOURLY, now=NOW, since=_hour(8), until=_hour(10))
    assert _due(got) == [_hour(8), _hour(9), _hour(10)]


def test_interval_gap_between_stamped_runs(session):
    base = datetime(2026, 10, 28, 11, 0, tzinfo=timezone.utc)
    _run(session, EVERY_10M, base)
    _run(session, EVERY_10M, base + STEP)
    # Beat was down: 11:20 and 11:30 never went out, 11:40 fired once.
    _run(session, EVERY_10M, base + 4 * STEP)
    _run(session, EVERY_10M, base + 5 * STEP)
    now = base + 5 * STEP + timedelta(minutes=2)
    assert _due(list_missed_runs(session, _app(), EVERY_10M, now=now)) == [base + 2 * STEP, base + 3 * STEP]


def test_interval_on_time_runs_with_send_lag_are_fine(session):
    base = datetime(2026, 10, 28, 11, 0, tzinfo=timezone.utc)
    for i in range(5):
        t = base + i * STEP + timedelta(seconds=2 * i)  # beat's grid drifts by its send lag
        _run(session, EVERY_10M, t)
    assert list_missed_runs(session, _app(), EVERY_10M, now=base + timedelta(minutes=42)) == []


def test_interval_slots_after_the_last_stamp_are_missing(session):
    base = datetime(2026, 10, 28, 11, 0, tzinfo=timezone.utc)
    _run(session, EVERY_10M, base)
    now = base + timedelta(minutes=33)  # 11:10, 11:20 due; 11:30 inside grace
    assert _due(list_missed_runs(session, _app(), EVERY_10M, now=now)) == [base + STEP, base + 2 * STEP]


def test_counts_cover_schedules_with_misses_only(session):
    _run(session, HOURLY, _hour(10))  # 11:00 and 12:00 missing at 12:30
    _run(session, EVERY_10M, NOW - STEP)  # last slot 12:30 is inside grace
    counts = missed_run_counts(session, _app(), NOW - timedelta(hours=24), now=NOW)
    assert counts == {HOURLY: 2}


def test_detection_is_read_only(session):
    _run(session, HOURLY, _hour(10))
    before = session.query(TaskRun).count()
    list_missed_runs(session, _app(), HOURLY, now=NOW)
    missed_run_counts(session, _app(), NOW - timedelta(days=1), now=NOW)
    assert session.query(TaskRun).count() == before
    assert not session.new and not session.dirty


# --- REST ------------------------------------------------------------------


def _client():
    config = CeleryManagerConfig(celery_app=_app(), db_url="sqlite:///:memory:", secret_key="test-secret")
    fastapi_app, factory = create_app(config)
    with factory() as s:
        create_user(s, "alice", "pw")
    client = TestClient(fastapi_app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, factory


def test_missed_endpoint_inactive_without_stamps():
    client, _ = _client()
    body = client.get(f"/api/schedules/{HOURLY}/missed").json()
    assert body == {"active": False, "tracking_since": None, "grace_seconds": 300, "missed": []}


def test_missed_endpoint_shape_and_list_count():
    client, factory = _client()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    with factory() as s:
        _run(s, HOURLY, now - timedelta(hours=5))
        _run(s, HOURLY, now - timedelta(hours=1))  # hours -4, -3, -2 missing
        _run(s, HOURLY, now)
    body = client.get(f"/api/schedules/{HOURLY}/missed").json()
    assert body["active"] is True
    assert body["tracking_since"].endswith("+00:00")
    assert body["grace_seconds"] == 300
    assert [m["due_at"] for m in body["missed"]] == [(now - timedelta(hours=h)).isoformat() for h in (4, 3, 2)]

    items = {i["name"]: i for i in client.get("/api/schedules").json()}
    assert items[HOURLY]["missed_count"] == 3
    assert items[EVERY_10M]["missed_count"] == 0


def test_missed_endpoint_404_for_unknown_schedule():
    client, _ = _client()
    assert client.get("/api/schedules/nope/missed").status_code == 404
