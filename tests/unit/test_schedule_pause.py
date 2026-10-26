"""Whole-schedule pause / resume: decorator enforcement, startup sync,
REST endpoints, audit, and the additive column upgrade for older databases."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from celery import Celery
from celery.schedules import schedule as interval_schedule
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.schedules import (
    list_upcoming_with_overrides,
    pause_schedule,
    resume_schedule,
    sync_periodic_schedules,
)
from celery_manager.db.models import (
    AuditLog,
    Base,
    PeriodicSchedule,
    ScheduleOverride,
    TaskRun,
)
from celery_manager.db.session import init_db
from celery_manager.decorator import configure, track

SCHEDULE = "demo-every-2h"


def _beat_app():
    app = Celery("test_app", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    app.conf.beat_schedule = {
        SCHEDULE: {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=2))},
    }
    return app


@pytest.fixture
def env():
    app = _beat_app()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    configure(session_factory=Session, eta_tolerance_seconds=60)
    with Session() as session:
        sync_periodic_schedules(session, app)

    calls = []

    @app.task(name="demo.echo", bind=True)
    @track(schedule_name=SCHEDULE)
    def echo(self, value):
        calls.append(value)
        return value

    return app, Session, echo, calls


def _statuses(Session):
    with Session() as session:
        return [r.status for r in session.query(TaskRun).order_by(TaskRun.id).all()]


# --- decorator enforcement -------------------------------------------------


def test_a_paused_schedules_run_is_recorded_skipped_and_the_body_does_not_run(env):
    _, Session, echo, calls = env
    with Session() as session:
        pause_schedule(session, SCHEDULE, "alice")

    echo.delay("x")
    echo.delay("y")

    assert calls == []
    assert _statuses(Session) == ["skipped", "skipped"]
    with Session() as session:
        assert {r.schedule_name for r in session.query(TaskRun).all()} == {SCHEDULE}


def test_resume_restores_normal_runs(env):
    _, Session, echo, calls = env
    with Session() as session:
        pause_schedule(session, SCHEDULE, "alice")
    echo.delay("missed")
    with Session() as session:
        resume_schedule(session, SCHEDULE, "alice")
    echo.delay("next")

    # The firing missed while paused stays skipped; nothing is caught up.
    assert calls == ["next"]
    assert _statuses(Session) == ["skipped", "succeeded"]


def test_pause_only_affects_the_named_schedule(env):
    app, Session, echo, calls = env
    other_calls = []

    @app.task(name="demo.other", bind=True)
    @track(schedule_name="other-schedule")
    def other(self):
        other_calls.append(1)

    with Session() as session:
        session.add(PeriodicSchedule(name="other-schedule", task_name="demo.other", schedule_spec="x"))
        session.commit()
        pause_schedule(session, SCHEDULE, "alice")

    other.delay()
    echo.delay("x")
    assert other_calls == [1]
    assert calls == []


def test_pause_check_uses_task_name_when_no_schedule_name_is_given(env):
    """Same effective name the occurrence-override check uses."""
    app, Session, _, _ = env
    calls = []

    @app.task(name="demo.unnamed", bind=True)
    @track()
    def unnamed(self):
        calls.append(1)

    with Session() as session:
        session.add(PeriodicSchedule(name="demo.unnamed", task_name="demo.unnamed", schedule_spec="x"))
        session.commit()
        pause_schedule(session, "demo.unnamed", "alice")

    unnamed.delay()
    assert calls == []
    assert _statuses(Session) == ["skipped"]


def test_a_paused_firing_consumes_the_occurrence_override_aimed_at_it(env):
    """Chosen interplay: an override targets one firing. When that firing
    happens while the whole schedule is paused it is skipped either way, and
    the override is consumed so no stale row lingers for after resume."""
    _, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name=SCHEDULE, target_time=datetime.now(timezone.utc),
            action="skip", created_by="alice",
        ))
        session.commit()
        pause_schedule(session, SCHEDULE, "alice")

    echo.delay("x")

    assert calls == []
    assert _statuses(Session) == ["skipped"]
    with Session() as session:
        assert session.query(ScheduleOverride).one().consumed is True


def test_a_paused_firing_leaves_overrides_for_other_firings_alone(env):
    _, Session, echo, _ = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name=SCHEDULE, target_time=datetime.now(timezone.utc) + timedelta(hours=2),
            action="skip", created_by="alice",
        ))
        session.commit()
        pause_schedule(session, SCHEDULE, "alice")

    echo.delay("x")

    with Session() as session:
        assert session.query(ScheduleOverride).one().consumed is False


def test_pause_check_fails_open_on_storage_errors(env, monkeypatch):
    from celery_manager import decorator as decorator_module

    _, _, echo, calls = env

    def boom(session, name):
        raise RuntimeError("db down")

    monkeypatch.setattr(decorator_module, "_schedule_is_paused", boom)
    echo.delay("x")
    assert calls == ["x"]


# --- control layer ---------------------------------------------------------


def test_startup_sync_preserves_a_pause(env):
    app, Session, _, _ = env
    with Session() as session:
        pause_schedule(session, SCHEDULE, "alice")
    with Session() as session:
        sync_periodic_schedules(session, app)
    with Session() as session:
        row = session.query(PeriodicSchedule).filter_by(name=SCHEDULE).one()
        assert row.paused_at is not None
        assert row.paused_by == "alice"
        assert row.task_name == "demo.echo"


def test_pause_and_resume_are_idempotent_and_audit_only_real_changes(env):
    _, Session, _, _ = env
    with Session() as session:
        first = pause_schedule(session, SCHEDULE, "alice")
        first_paused_at = first.paused_at
        again = pause_schedule(session, SCHEDULE, "bob")
        assert again.paused_by == "alice"
        assert again.paused_at == first_paused_at

        resume_schedule(session, SCHEDULE, "bob")
        resumed = resume_schedule(session, SCHEDULE, "bob")
        assert resumed.paused_at is None and resumed.paused_by is None

        actions = [(a.action, a.username) for a in session.query(AuditLog).order_by(AuditLog.id).all()]
        assert actions == [("pause_schedule", "alice"), ("resume_schedule", "bob")]
        resume_row = session.query(AuditLog).filter_by(action="resume_schedule").one()
        assert json.loads(resume_row.details)["paused_by"] == "alice"


def test_pause_and_resume_return_none_for_an_unknown_schedule(env):
    _, Session, _, _ = env
    with Session() as session:
        assert pause_schedule(session, "nope", "alice") is None
        assert resume_schedule(session, "nope", "alice") is None
        assert session.query(AuditLog).count() == 0


def test_occurrences_are_marked_paused_while_the_schedule_is(env):
    app, Session, _, _ = env
    with Session() as session:
        assert all(o["paused"] is False for o in list_upcoming_with_overrides(session, app, SCHEDULE))
        pause_schedule(session, SCHEDULE, "alice")
        occurrences = list_upcoming_with_overrides(session, app, SCHEDULE)
    assert occurrences and all(o["paused"] is True for o in occurrences)
    assert all(o["action"] is None for o in occurrences)


# --- REST ------------------------------------------------------------------


def _client():
    config = CeleryManagerConfig(celery_app=_beat_app(), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client


def test_pause_endpoint_pauses_and_list_exposes_paused_fields():
    client = _client()
    listed = client.get("/api/schedules").json()[0]
    assert listed["paused"] is False
    assert listed["paused_at"] is None and listed["paused_by"] is None

    response = client.post(f"/api/schedules/{SCHEDULE}/pause")
    assert response.status_code == 200
    body = response.json()
    assert body["paused"] is True and body["paused_by"] == "alice"

    listed = client.get("/api/schedules").json()[0]
    assert listed["paused"] is True
    assert listed["paused_by"] == "alice"
    assert listed["paused_at"].endswith("+00:00")  # UTC-stamped at the API boundary

    occurrences = client.get(f"/api/schedules/{SCHEDULE}/occurrences").json()
    assert occurrences and all(o["paused"] is True for o in occurrences)


def test_pause_and_resume_endpoints_are_idempotent_and_audited():
    client = _client()
    first = client.post(f"/api/schedules/{SCHEDULE}/pause").json()
    second = client.post(f"/api/schedules/{SCHEDULE}/pause")
    assert second.status_code == 200
    assert second.json()["paused_at"] == first["paused_at"]

    assert client.post(f"/api/schedules/{SCHEDULE}/resume").json()["paused"] is False
    again = client.post(f"/api/schedules/{SCHEDULE}/resume")
    assert again.status_code == 200
    assert (again.json()["paused"], again.json()["paused_at"], again.json()["paused_by"]) == (False, None, None)

    rows = client.get("/api/audit").json()
    pause_rows = [r for r in rows if r["action"] == "pause_schedule"]
    resume_rows = [r for r in rows if r["action"] == "resume_schedule"]
    assert len(pause_rows) == 1 and len(resume_rows) == 1
    assert pause_rows[0]["target_type"] == "schedule"
    assert pause_rows[0]["target_id"] == SCHEDULE
    assert pause_rows[0]["username"] == "alice"


@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_pause_and_resume_404_for_an_unknown_schedule(verb):
    client = _client()
    assert client.post(f"/api/schedules/nope/{verb}").status_code == 404


@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_pause_and_resume_require_auth(verb):
    config = CeleryManagerConfig(celery_app=_beat_app(), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    client = TestClient(app, base_url="https://testserver")
    assert client.post(f"/api/schedules/{SCHEDULE}/{verb}").status_code == 401


@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_pause_and_resume_refuse_cross_origin_requests(verb):
    client = _client()
    response = client.post(f"/api/schedules/{SCHEDULE}/{verb}", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.get("/api/schedules").json()[0]["paused"] is False


# --- additive column upgrade -----------------------------------------------


def test_init_db_adds_pause_columns_to_an_older_database(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(db_url)
    with old.begin() as conn:
        conn.execute(text(
            "CREATE TABLE periodic_schedules (id INTEGER PRIMARY KEY, name VARCHAR(255) UNIQUE, "
            "task_name VARCHAR(255), schedule_spec VARCHAR(255), args TEXT, kwargs TEXT, enabled BOOLEAN)"
        ))
        conn.execute(text(
            "INSERT INTO periodic_schedules (name, task_name, schedule_spec, args, kwargs, enabled) "
            "VALUES ('legacy', 'demo.echo', 'x', '[]', '{}', 1)"
        ))
    old.dispose()

    engine = init_db(CeleryManagerConfig(celery_app=Celery("t"), db_url=db_url, secret_key="s"))
    columns = {c["name"] for c in inspect(engine).get_columns("periodic_schedules")}
    assert {"paused_at", "paused_by"} <= columns

    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        row = pause_schedule(session, "legacy", "alice")
        assert row.paused_by == "alice"

    # Running it again against an up-to-date database is a no-op.
    init_db(CeleryManagerConfig(celery_app=Celery("t"), db_url=db_url, secret_key="s"))
