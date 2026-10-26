# ruff: noqa: F811
"""Pausing a schedule until a set time: decorator enforcement before/after the
deadline, lazy auto-resume (exactly once, audited), validation, re-pausing
with a new deadline, per-occurrence flags, and the REST fields."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from celery_manager.control.schedules import (
    expire_stale_pause,
    list_periodic_schedules,
    list_upcoming_with_overrides,
    pause_schedule,
    resume_schedule,
)
from celery_manager.db.models import AuditLog, PeriodicSchedule

# Reuse the fixtures/helpers of the whole-schedule pause tests.
from tests.unit.test_schedule_pause import SCHEDULE, _client, _statuses, env  # noqa: F401


def _in(**kw):
    return datetime.now(timezone.utc) + timedelta(**kw)


def _expire_now(Session):
    """Rewrite paused_until into the past, as if the deadline had elapsed."""
    with Session() as session:
        row = session.query(PeriodicSchedule).filter_by(name=SCHEDULE).one()
        row.paused_until = datetime.now(timezone.utc) - timedelta(seconds=5)
        session.commit()


def _audit(Session, action):
    with Session() as session:
        return [r for r in session.query(AuditLog).order_by(AuditLog.id).all() if r.action == action]


def test_decorator_skips_while_until_is_in_the_future(env):
    _, Session, echo, calls = env
    with Session() as session:
        pause_schedule(session, SCHEDULE, "alice", until=_in(hours=1))
    echo.delay("x")
    assert calls == [] and _statuses(Session) == ["skipped"]


def test_decorator_runs_normally_once_until_has_passed_and_clears_the_pause(env):
    _, Session, echo, calls = env
    with Session() as session:
        pause_schedule(session, SCHEDULE, "alice", until=_in(hours=1))
    _expire_now(Session)

    echo.delay("x")
    echo.delay("y")

    assert calls == ["x", "y"] and _statuses(Session) == ["succeeded", "succeeded"]
    with Session() as session:
        row = session.query(PeriodicSchedule).one()
        assert (row.paused_at, row.paused_by, row.paused_until) == (None, None, None)
    (audit,) = _audit(Session, "resume_schedule")
    details = json.loads(audit.details)
    assert audit.username == "system" and details["automatic"] is True and details["paused_by"] == "alice"


def test_auto_resume_clears_and_audits_exactly_once(env):
    _, Session, _, _ = env
    with Session() as session:
        pause_schedule(session, SCHEDULE, "alice", until=_in(hours=1))
    _expire_now(Session)
    with Session() as session:
        stale = session.query(PeriodicSchedule.paused_until).scalar()

    # Two readers that both saw the stale value: only one wins the UPDATE.
    with Session() as s1, Session() as s2:
        results = [expire_stale_pause(s1, SCHEDULE, stale), expire_stale_pause(s2, SCHEDULE, stale)]
    assert results == [True, False]
    assert len(_audit(Session, "resume_schedule")) == 1


def test_list_clears_a_stale_pause_once(env):
    _, Session, _, _ = env
    with Session() as session:
        pause_schedule(session, SCHEDULE, "alice", until=_in(hours=1))
    _expire_now(Session)
    with Session() as session:
        assert list_periodic_schedules(session)[0].paused_at is None
    with Session() as session:
        list_periodic_schedules(session)
        resume_schedule(session, SCHEDULE, "bob")
    assert len(_audit(Session, "resume_schedule")) == 1


def test_a_past_until_is_rejected(env):
    _, Session, _, _ = env
    with Session() as session:
        with pytest.raises(ValueError):
            pause_schedule(session, SCHEDULE, "alice", until=_in(minutes=-1))
        assert session.query(PeriodicSchedule).one().paused_at is None
        assert session.query(AuditLog).count() == 0


def test_naive_until_is_utc(env):
    _, Session, _, _ = env
    naive = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
    with Session() as session:
        row = pause_schedule(session, SCHEDULE, "alice", until=naive)
        assert row.paused_until.replace(tzinfo=timezone.utc) == naive.replace(tzinfo=timezone.utc)


def test_repausing_updates_until_and_audits_the_change(env):
    _, Session, _, _ = env
    first, second = _in(hours=1), _in(hours=3)
    with Session() as session:
        a = pause_schedule(session, SCHEDULE, "alice", until=first)
        paused_at = a.paused_at
        pause_schedule(session, SCHEDULE, "bob", until=first)  # same until: no-op
        b = pause_schedule(session, SCHEDULE, "bob", until=second)
        assert b.paused_by == "alice" and b.paused_at == paused_at
        assert b.paused_until.replace(tzinfo=timezone.utc) == second
        c = pause_schedule(session, SCHEDULE, "bob")  # indefinite
        assert c.paused_until is None
    rows = _audit(Session, "pause_schedule")
    assert len(rows) == 3
    assert json.loads(rows[1].details)["updated"] is True
    assert json.loads(rows[1].details)["previous_until"] == first.isoformat()
    assert json.loads(rows[2].details)["until"] is None


def test_repausing_after_expiry_starts_a_fresh_pause(env):
    _, Session, _, _ = env
    with Session() as session:
        pause_schedule(session, SCHEDULE, "alice", until=_in(hours=1))
    _expire_now(Session)
    with Session() as session:
        row = pause_schedule(session, SCHEDULE, "bob")
        assert row.paused_by == "bob" and row.paused_until is None
    assert len(_audit(Session, "resume_schedule")) == 1


def test_occurrence_paused_flag_is_per_occurrence(env):
    app, Session, _, _ = env
    until = _in(hours=5)  # occurrences are every 2h
    with Session() as session:
        pause_schedule(session, SCHEDULE, "alice", until=until)
        occ = list_upcoming_with_overrides(session, app, SCHEDULE, count=6)
    flags = [o["paused"] for o in occ]
    assert flags == [o["target_time"] < until for o in occ]
    assert True in flags and False in flags


def test_api_pause_with_until_and_list_fields():
    client = _client()
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    response = client.post(f"/api/schedules/{SCHEDULE}/pause", json={"until": until.isoformat()})
    assert response.status_code == 200
    body = response.json()
    assert body["paused"] is True and body["paused_until"].endswith("+00:00")
    assert datetime.fromisoformat(body["paused_until"]) == until
    listed = client.get("/api/schedules").json()[0]
    assert listed["paused"] is True and listed["paused_until"] == body["paused_until"]

    assert client.post(f"/api/schedules/{SCHEDULE}/resume").json()["paused_until"] is None
    assert client.post(f"/api/schedules/{SCHEDULE}/pause").json()["paused_until"] is None


def test_api_rejects_a_past_until_with_422():
    client = _client()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert client.post(f"/api/schedules/{SCHEDULE}/pause", json={"until": past}).status_code == 422
    assert client.get("/api/schedules").json()[0]["paused"] is False


def test_api_reports_an_elapsed_pause_as_not_paused():
    client = _client()
    soon = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert client.post(f"/api/schedules/{SCHEDULE}/pause", json={"until": soon.isoformat()}).status_code == 200
    time.sleep(1.2)
    listed = client.get("/api/schedules").json()[0]
    assert listed["paused"] is False and listed["paused_until"] is None
    rows = [r for r in client.get("/api/audit").json() if r["action"] == "resume_schedule"]
    assert len(rows) == 1
