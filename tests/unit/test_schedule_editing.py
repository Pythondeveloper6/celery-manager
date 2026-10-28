"""Editing a schedule's timing from the dashboard.

An operator's crontab/interval edit is stored beside the code's
`beat_schedule` entry (`periodic_schedules.schedule_override`), previewed
before saving, used everywhere occurrences are projected, applied by
`celery_manager.beat:DatabaseScheduler` without a beat restart, and only
accepted by the API when `schedule_editing` says beat will apply it."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from celery import Celery
from celery.schedules import crontab
from celery.schedules import schedule as interval_schedule
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.beat import DatabaseScheduler, occurrence_headers
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.missed import list_missed_runs
from celery_manager.control.schedules import (
    edit_schedule,
    effective_schedule,
    list_upcoming_with_overrides,
    pause_occurrence,
    revert_schedule,
    sync_periodic_schedules,
)
from celery_manager.db.migrate import alembic_config, upgrade_database
from celery_manager.db.models import AuditLog, PeriodicSchedule, ScheduleOverride, TaskRun
from celery_manager.db.session import get_session_factory, init_db
from celery_manager.decorator import configure, track
from celery_manager.schedule_specs import (
    ScheduleSpecError,
    spec_from_schedule,
    validate_spec,
)

NAME = "demo-every-2h"
EVERY = timedelta(hours=2)
CRON_3AM = {"type": "crontab", "minute": "0", "hour": "3"}


def _app() -> Celery:
    app = Celery("test_editing")
    app.conf.beat_schedule = {
        NAME: {"task": "demo.echo", "schedule": interval_schedule(run_every=EVERY)},
    }
    return app


# --- specs ---------------------------------------------------------------------


def test_crontab_spec_is_normalized_and_parsed_by_celery():
    spec, sched = validate_spec({"type": "crontab", "minute": " */15 ", "day_of_week": "mon-fri"})
    assert spec == {
        "type": "crontab", "minute": "*/15", "hour": "*",
        "day_of_week": "mon-fri", "day_of_month": "*", "month_of_year": "*",
    }
    assert sched == crontab(minute="*/15", day_of_week="mon-fri")


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"type": "crontab", "minute": "61"}, "Minute '61'"),
        ({"type": "crontab", "hour": "abc"}, "Hour 'abc'"),
        ({"type": "crontab", "minute": "*/0"}, "Minute"),
        ({"type": "crontab", "day_of_month": "31", "month_of_year": "2"}, "never fires"),
        ({"type": "crontab", "second": "1"}, "Unknown crontab field"),
        ({"type": "interval", "seconds": 0.5}, "at least 1 second"),
        ({"type": "interval", "seconds": "10"}, "number of seconds"),
        ({"type": "interval", "seconds": 10**9}, "at most 366 days"),
        ({"type": "solar"}, '"crontab" or "interval"'),
        ([1, 2], "JSON object"),
    ],
)
def test_invalid_specs_are_refused_with_a_sentence(data, fragment):
    with pytest.raises(ScheduleSpecError) as exc:
        validate_spec(data)
    assert fragment in str(exc.value)


def test_code_schedule_converts_to_a_spec_for_the_editor():
    assert spec_from_schedule(interval_schedule(run_every=EVERY)) == {"type": "interval", "seconds": 7200}
    assert spec_from_schedule(crontab(minute=0, hour="*/2", day_of_week={1, 2}))["day_of_week"] == "1,2"


# --- control layer ---------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    app = _app()
    url = f"sqlite:///{tmp_path / 'cm.db'}"
    engine = init_db(CeleryManagerConfig(celery_app=app, db_url=url, secret_key="s"))
    Session = get_session_factory(engine)
    with Session() as s:
        sync_periodic_schedules(s, app)
    return app, Session, url


def test_edit_and_revert_are_stored_and_audited(db):
    app, Session, _ = db
    with Session() as s:
        row, unmatched = edit_schedule(s, app, NAME, CRON_3AM, "alice")
        assert unmatched == []
        assert json.loads(row.schedule_override)["hour"] == "3"
        assert row.edited_by == "alice" and row.edited_at is not None
        # Same spec again: no-op, no second audit row.
        edit_schedule(s, app, NAME, CRON_3AM, "bob")
        assert s.get(PeriodicSchedule, row.id).edited_by == "alice"

        revert_schedule(s, NAME, "carol")
        revert_schedule(s, NAME, "carol")  # idempotent
        row = s.query(PeriodicSchedule).filter_by(name=NAME).one()
        assert (row.schedule_override, row.edited_by, row.edited_at) == (None, None, None)

        audit = s.query(AuditLog).order_by(AuditLog.id).all()
        assert [(a.action, a.username) for a in audit] == [("edit_schedule", "alice"), ("revert_schedule", "carol")]
        edit = json.loads(audit[0].details)
        assert edit["before"] is None and edit["after"]["hour"] == "3"
        assert edit["code"] == "<freq: 2.00 hours>"
        assert json.loads(audit[1].details)["before"]["hour"] == "3"


def test_unknown_schedule_is_none(db):
    app, Session, _ = db
    with Session() as s:
        assert edit_schedule(s, app, "nope", CRON_3AM, "alice") is None
        assert revert_schedule(s, "nope", "alice") is None


def test_sync_never_clobbers_an_edit(db):
    app, Session, _ = db
    with Session() as s:
        edit_schedule(s, app, NAME, CRON_3AM, "alice")
        sync_periodic_schedules(s, app)  # what every API restart does
        row = s.query(PeriodicSchedule).filter_by(name=NAME).one()
        assert json.loads(row.schedule_override)["hour"] == "3"
        assert row.edited_by == "alice"
        assert row.schedule_spec == "<freq: 2.00 hours>"  # still the code's


def test_upcoming_occurrences_follow_the_edit(db):
    app, Session, _ = db
    with Session() as s:
        edit_schedule(s, app, NAME, CRON_3AM, "alice")
        listed = list_upcoming_with_overrides(s, app, NAME, count=5)
        assert [(o["target_time"].hour, o["target_time"].minute) for o in listed] == [(3, 0)] * 5
        # The single place: `effective_schedule` agrees.
        assert effective_schedule(s, NAME, app.conf.beat_schedule[NAME]) == crontab(minute="0", hour="3")
        # Not applied while editing is off: the code's schedule is projected.
        code = list_upcoming_with_overrides(s, app, NAME, count=2, apply_edits=False)
        assert code[1]["target_time"] - code[0]["target_time"] == EVERY
        assert effective_schedule(s, NAME, app.conf.beat_schedule[NAME], apply_edits=False).run_every == EVERY


def test_skips_the_new_schedule_has_no_run_for_are_kept_and_reported(db):
    app, Session, _ = db
    with Session() as s:
        edit_schedule(s, app, NAME, CRON_3AM, "alice")
        three_am = list_upcoming_with_overrides(s, app, NAME, count=1)[0]["target_time"]
        pause_occurrence(s, NAME, three_am, "skip", "alice")
        _row, unmatched = edit_schedule(s, app, NAME, {"type": "crontab", "minute": "30", "hour": "4"}, "alice")
        assert unmatched == [three_am]
        assert s.query(ScheduleOverride).filter_by(consumed=False).count() == 1
        # Back to 03:00: the skip is in force again.
        _row, unmatched = edit_schedule(s, app, NAME, CRON_3AM, "alice")
        assert unmatched == []
        assert list_upcoming_with_overrides(s, app, NAME, count=1)[0]["action"] == "skip"


# --- API ---------------------------------------------------------------------------


def _clients(schedule_editing: bool = True):
    config = CeleryManagerConfig(
        celery_app=_app(), db_url="sqlite:///:memory:", secret_key="test-secret",
        schedule_editing=schedule_editing,
    )
    app, session_factory = create_app(config)
    clients = {}
    with session_factory() as session:
        for role in ("viewer", "operator"):
            create_user(session, role, "pw", role=role)
    for role in ("viewer", "operator"):
        client = TestClient(app, base_url="https://testserver")
        assert client.post("/api/auth/login", json={"username": role, "password": "pw"}).status_code == 200
        clients[role] = client
    return clients, session_factory


def _item(client) -> dict:
    return next(s for s in client.get("/api/schedules").json() if s["name"] == NAME)


def test_preview_returns_the_next_five_crontab_runs():
    clients, _ = _clients()
    response = clients["viewer"].post(f"/api/schedules/{NAME}/preview", json={"type": "crontab", "minute": "0", "hour": "*/6"})
    assert response.status_code == 200
    times = [datetime.fromisoformat(t) for t in response.json()["occurrences"]]
    now = datetime.now(timezone.utc)
    assert len(times) == 5
    assert all(t.minute == 0 and t.hour % 6 == 0 for t in times)
    assert now < times[0] <= now + timedelta(hours=6)
    assert all(b - a == timedelta(hours=6) for a, b in zip(times, times[1:]))


def test_interval_preview_is_projected_from_the_last_run():
    clients, session_factory = _clients()
    last = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=250)
    with session_factory() as s:
        s.add(TaskRun(task_id="t1", task_name="demo.echo", status="succeeded", schedule_name=NAME,
                      started_at=last, beat_sent_at=last))
        s.commit()
    response = clients["viewer"].post(f"/api/schedules/{NAME}/preview", json={"type": "interval", "seconds": 100})
    times = [datetime.fromisoformat(t) for t in response.json()["occurrences"]]
    # Beat keeps last_run_at across the edit: last + k * 100s, from the next one ahead.
    assert times == [last + timedelta(seconds=100 * k) for k in range(3, 8)]


def test_invalid_spec_is_a_422_with_a_readable_message():
    clients, _ = _clients()
    for path, method in ((f"/api/schedules/{NAME}/preview", "POST"), (f"/api/schedules/{NAME}/schedule", "PUT")):
        response = clients["operator"].request(method, path, json={"type": "crontab", "minute": "99"})
        assert response.status_code == 422
        assert response.json()["detail"].startswith("Minute '99' is not valid")


def test_unknown_schedule_is_a_404():
    clients, _ = _clients()
    assert clients["viewer"].post("/api/schedules/nope/preview", json=CRON_3AM).status_code == 404
    assert clients["operator"].put("/api/schedules/nope/schedule", json=CRON_3AM).status_code == 404
    assert clients["operator"].delete("/api/schedules/nope/schedule").status_code == 404


def test_edit_and_revert_over_the_api():
    clients, _ = _clients()
    op = clients["operator"]
    before = _item(op)
    assert before["schedule_override"] is None and before["edited_by"] is None
    assert before["code_schedule"] == {"type": "interval", "seconds": 7200}
    assert before["schedule_editing"] is True

    response = op.put(f"/api/schedules/{NAME}/schedule", json=CRON_3AM)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schedule_override"]["hour"] == "3"
    assert body["unmatched_overrides"] == []
    item = _item(op)
    assert item["schedule_spec"].startswith("<crontab: 0 3 ")
    assert item["code_schedule_spec"] == "<freq: 2.00 hours>"
    assert item["edited_by"] == "operator" and item["edited_at"].endswith("+00:00")
    upcoming = op.get(f"/api/schedules/{NAME}/occurrences").json()
    assert all(datetime.fromisoformat(o["target_time"]).hour == 3 for o in upcoming)

    assert op.delete(f"/api/schedules/{NAME}/schedule").status_code == 200
    item = _item(op)
    assert item["schedule_override"] is None and item["schedule_spec"] == "<freq: 2.00 hours>"
    actions = [r["action"] for r in op.get("/api/audit").json()]
    assert "edit_schedule" in actions and "revert_schedule" in actions


def test_editing_disabled_is_a_409_but_preview_and_revert_still_work():
    clients, session_factory = _clients(schedule_editing=False)
    op = clients["operator"]
    response = op.put(f"/api/schedules/{NAME}/schedule", json=CRON_3AM)
    assert response.status_code == 409
    assert "DatabaseScheduler" in response.json()["detail"]
    assert _item(op)["schedule_editing"] is False
    assert op.post(f"/api/schedules/{NAME}/preview", json=CRON_3AM).status_code == 200

    # An edit left over from when editing was on: listed but not projected,
    # and it can still be cleared.
    with session_factory() as s:
        edit_schedule(s, op.app.state.cm_config.celery_app, NAME, CRON_3AM, "operator")
    item = _item(op)
    assert item["schedule_override"]["hour"] == "3"
    assert item["schedule_spec"] == "<freq: 2.00 hours>"
    assert op.delete(f"/api/schedules/{NAME}/schedule").status_code == 200
    assert _item(op)["schedule_override"] is None


def test_viewers_can_preview_but_not_edit():
    clients, _ = _clients()
    viewer = clients["viewer"]
    assert viewer.post(f"/api/schedules/{NAME}/preview", json=CRON_3AM).status_code == 200
    for method in ("PUT", "DELETE"):
        response = viewer.request(method, f"/api/schedules/{NAME}/schedule", json=CRON_3AM if method == "PUT" else None)
        assert response.status_code == 403
        assert "operator" in response.json()["detail"]


# --- beat: DatabaseScheduler -----------------------------------------------------


@pytest.fixture
def make_scheduler(db, tmp_path):
    """Build DatabaseSchedulers on one shelve file; closes them afterwards
    (an unclosed shelf tries to pickle its entries at interpreter exit)."""
    app, _Session, url = db
    app.conf.celery_manager_db_url = url
    built = []

    def build(refresh=None):
        if refresh is not None:
            app.conf.celery_manager_schedule_refresh = refresh
        sched = DatabaseScheduler(app=app, schedule_filename=str(tmp_path / "beat-schedule"))
        built.append(sched)
        return sched

    yield build
    for sched in built:
        if sched._store is not None:
            sched._store.close()


def test_database_scheduler_needs_the_db_url(tmp_path):
    with pytest.raises(RuntimeError, match="celery_manager_db_url"):
        DatabaseScheduler(app=_app(), schedule_filename=str(tmp_path / "beat"))


def test_database_scheduler_picks_up_an_edit_and_keeps_last_run_at(db, make_scheduler):
    app, Session, _ = db
    sched = make_scheduler()
    last = datetime.now(timezone.utc) - timedelta(minutes=5)
    sched.schedule[NAME].last_run_at = last
    sched.schedule[NAME].total_run_count = 7
    assert sched.schedule[NAME].schedule.run_every == EVERY

    with Session() as s:
        edit_schedule(s, app, NAME, {"type": "interval", "seconds": 600}, "alice")
    assert sched.refresh_schedule_edits() is True
    entry = sched.schedule[NAME]
    assert entry.schedule.run_every == timedelta(seconds=600)
    assert (entry.last_run_at, entry.total_run_count) == (last, 7)
    assert entry.is_due().next <= 300  # last + 10 min, not now + 10 min
    assert sched.refresh_schedule_edits() is False  # nothing new

    with Session() as s:
        revert_schedule(s, NAME, "alice")
    sched.refresh_schedule_edits()
    assert sched.schedule[NAME].schedule.run_every == EVERY
    assert sched.schedule[NAME].last_run_at == last


def test_database_scheduler_refreshes_on_tick_and_wakes_up_for_it(db, make_scheduler):
    app, Session, _ = db
    sched = make_scheduler(refresh=5)
    sched.schedule[NAME].last_run_at = datetime.now(timezone.utc)
    assert sched.tick() <= 5  # an entry due in 2h does not let beat sleep past a refresh

    with Session() as s:
        edit_schedule(s, app, NAME, CRON_3AM, "alice")
    sched.tick()  # refresh not due yet
    assert sched.schedule[NAME].schedule.run_every == EVERY
    sched._cm_last_refresh = time.monotonic() - 10
    sched.tick()
    assert sched.schedule[NAME].schedule == crontab(minute="0", hour="3")


def test_database_scheduler_reapplies_edits_after_a_restart(db, make_scheduler):
    app, Session, _ = db
    with Session() as s:
        edit_schedule(s, app, NAME, {"type": "interval", "seconds": 600}, "alice")
    sched = make_scheduler()
    assert sched.schedule[NAME].schedule.run_every == timedelta(seconds=600)
    last = datetime.now(timezone.utc) - timedelta(minutes=1)
    sched.schedule[NAME].last_run_at = last
    sched.close()
    sched._store = None

    again = make_scheduler()
    assert again.schedule[NAME].schedule.run_every == timedelta(seconds=600)
    assert again.schedule[NAME].last_run_at == last


def test_database_scheduler_keeps_firing_when_the_db_is_unreadable(db, make_scheduler, monkeypatch):
    app, Session, _ = db
    with Session() as s:
        edit_schedule(s, app, NAME, CRON_3AM, "alice")
    sched = make_scheduler()

    def broken():
        raise OSError("database is down")

    monkeypatch.setattr(sched, "_cm_read_edits", broken)
    assert sched.refresh_schedule_edits() is False
    assert sched.schedule[NAME].schedule == crontab(minute="0", hour="3")  # what it had


def test_decorator_matches_the_edited_interval_beat_stamps(db, make_scheduler):
    """The worker matches a skip against the occurrence and interval beat
    stamps, so with DatabaseScheduler the edited schedule is what counts:
    a skip 45s off the stamped time matched under the 2h code interval
    (window 60s) but not under a 60s edit (window capped at 30s)."""
    app, Session, _ = db
    app.conf.task_always_eager = True
    configure(session_factory=Session, eta_tolerance_seconds=60)
    calls = []

    @app.task(name="demo.echo", bind=True)
    @track(schedule_name=NAME)
    def echo(self, value):
        calls.append(value)

    with Session() as s:
        edit_schedule(s, app, NAME, {"type": "interval", "seconds": 60}, "alice")
    sched = make_scheduler()
    entry = sched.schedule[NAME]
    entry.last_run_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    headers = occurrence_headers(entry)
    assert headers["cm_interval"] == 60.0
    occurrence = datetime.fromisoformat(headers["cm_occurrence"])

    with Session() as s:
        s.add(ScheduleOverride(schedule_name=NAME, target_time=occurrence + timedelta(seconds=45),
                               action="skip", created_by="alice", created_at=datetime.now(timezone.utc)))
        s.commit()
    echo.apply(args=["far"], headers=headers)
    assert calls == ["far"]

    with Session() as s:
        s.add(ScheduleOverride(schedule_name=NAME, target_time=occurrence + timedelta(seconds=10),
                               action="skip", created_by="alice", created_at=datetime.now(timezone.utc)))
        s.commit()
    echo.apply(args=["near"], headers=headers)
    assert calls == ["far"]


# --- migration ---------------------------------------------------------------------


def test_migration_0008_adds_and_drops_the_edit_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cm.db'}")
    upgrade_database(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("periodic_schedules")}
    assert {"schedule_override", "edited_by", "edited_at"} <= columns
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "0007")
    columns = {c["name"] for c in inspect(engine).get_columns("periodic_schedules")}
    assert not {"schedule_override", "edited_by", "edited_at"} & columns
    assert {"paused_at", "paused_until"} <= columns
    engine.dispose()


# --- missed-run detection ------------------------------------------------------


def test_missed_runs_judge_the_edited_schedule_from_the_edit_on(db):
    """Hourly at :00 in the code, edited at 09:30 to every two hours at :30.
    The 12:30 firing of the edit never arrived; the code's 10:00-12:00
    slots are not missed, because beat was not firing them."""
    app, Session, _ = db
    app.conf.beat_schedule = {NAME: {"task": "demo.echo", "schedule": crontab(minute=0)}}

    def at(h, m=0):
        return datetime(2026, 10, 28, h, m, tzinfo=timezone.utc)

    with Session() as s:
        for t in (at(8), at(9), at(10, 30)):
            s.add(TaskRun(task_id=t.isoformat(), task_name="demo.echo", status="succeeded",
                          schedule_name=NAME, started_at=t, occurrence_at=t, beat_sent_at=t))
        row = s.query(PeriodicSchedule).filter_by(name=NAME).one()
        row.schedule_override = json.dumps({"type": "crontab", "minute": "30", "hour": "*/2"})
        row.edited_at = at(9, 30)
        s.commit()

        now = at(13)
        assert [r["due_at"] for r in list_missed_runs(s, app, NAME, now=now)] == [at(12, 30)]
        code = list_missed_runs(s, app, NAME, now=now, apply_edits=False)
        assert [r["due_at"] for r in code] == [at(10), at(11), at(12)]
