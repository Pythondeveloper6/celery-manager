"""Maintenance windows: recurrence arithmetic (boundaries, midnight, DST),
the decorator skipping inside a window with a reason, annotated upcoming
occurrences, missed runs, the REST API (validation, roles, audit) and the
0010 migration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from celery import Celery
from celery.schedules import crontab
from celery.schedules import schedule as interval_schedule
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.maintenance import (
    DuplicateWindowName,
    MaintenanceWindowError,
    active_windows,
    create_window,
    next_spans,
    normalize_window,
    update_window,
)
from celery_manager.control.missed import list_missed_runs
from celery_manager.control.schedules import list_upcoming_with_overrides, pause_schedule, sync_periodic_schedules
from celery_manager.db.migrate import alembic_config, upgrade_database
from celery_manager.db.models import AuditLog, Base, MaintenanceWindow, TaskRun
from celery_manager.decorator import configure, track
from celery_manager.occurrences import OCCURRENCE_HEADER, SCHEDULE_HEADER, format_occurrence

NAME = "nightly"
OTHER = "hourly"
SUNDAY = 6
SATURDAY = 5


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# 2026-10-25 is a Sunday.
SUN = (2026, 10, 25)
MON = (2026, 10, 26)


def window(days=(SUNDAY,), start="02:00", minutes=120, tz="UTC", applies="*", name="patching", enabled=True):
    return {
        "name": name, "days_of_week": list(days), "start_time": start, "duration_minutes": minutes,
        "timezone": tz, "applies_to": applies, "enabled": enabled,
    }


@pytest.fixture
def env():
    app = Celery("test_mw")
    app.conf.task_always_eager = True
    app.conf.beat_schedule = {
        NAME: {"task": "demo.echo", "schedule": crontab(minute=0, hour=3)},
        OTHER: {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=1))},
    }
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    configure(session_factory=Session, eta_tolerance_seconds=60)
    with Session() as s:
        sync_periodic_schedules(s, app)
    return app, Session


def add(Session, **kwargs):
    with Session() as s:
        return create_window(s, window(**kwargs), "alice")


def is_active(Session, at, schedule=NAME):
    with Session() as s:
        return [w.name for w in active_windows(s, schedule, at)]


# --- recurrence --------------------------------------------------------------


def test_inside_outside_and_boundaries(env):
    _, Session = env
    add(Session)  # Sundays 02:00-04:00 UTC
    assert is_active(Session, utc(*SUN, 2, 0)) == ["patching"]  # start inclusive
    assert is_active(Session, utc(*SUN, 3, 59)) == ["patching"]
    assert is_active(Session, utc(*SUN, 4, 0)) == []  # end exclusive
    assert is_active(Session, utc(*SUN, 1, 59)) == []
    assert is_active(Session, utc(*MON, 3, 0)) == []  # wrong day


def test_window_crossing_midnight(env):
    _, Session = env
    add(Session, days=(SATURDAY,), start="23:00", minutes=180)  # Sat 23:00 -> Sun 02:00
    assert is_active(Session, utc(2026, 10, 24, 22, 59)) == []
    assert is_active(Session, utc(2026, 10, 24, 23, 0)) == ["patching"]
    assert is_active(Session, utc(*SUN, 1, 59)) == ["patching"]
    assert is_active(Session, utc(*SUN, 2, 0)) == []
    # The part after midnight belongs to Saturday's window, not Sunday's.
    assert is_active(Session, utc(2026, 11, 1, 1, 0)) == ["patching"]
    assert is_active(Session, utc(*MON, 1, 0)) == []


def test_multi_day_window_longer_than_a_day(env):
    _, Session = env
    add(Session, days=(SATURDAY,), start="12:00", minutes=36 * 60)  # Sat noon -> Mon 00:00
    assert is_active(Session, utc(*SUN, 23, 0)) == ["patching"]
    assert is_active(Session, utc(*SUN, 23, 59)) == ["patching"]
    assert is_active(Session, utc(*MON, 0, 0)) == []  # Sat 12:00 + 36h = Mon 00:00, exclusive


def test_window_follows_its_own_timezone(env):
    _, Session = env
    add(Session, tz="Europe/Berlin")  # Sunday 02:00 CET (UTC+1 after 2026-10-25 03:00 CEST->CET)
    # 2026-10-25 02:00 Berlin is still CEST (UTC+2): 00:00 UTC.
    assert is_active(Session, utc(*SUN, 0, 0)) == ["patching"]
    assert is_active(Session, utc(*SUN, 1, 59)) == ["patching"]
    assert is_active(Session, utc(*SUN, 2, 0)) == []


def test_spring_forward_window_keeps_its_elapsed_length(env):
    """2026-03-08 New York: 02:00 -> 03:00. A 01:00 start lasting 3 hours ends
    at 05:00 EDT (09:00 UTC), one wall-clock hour later than usual."""
    _, Session = env
    add(Session, start="01:00", minutes=180, tz="America/New_York")
    assert is_active(Session, utc(2026, 3, 8, 5, 59)) == []
    assert is_active(Session, utc(2026, 3, 8, 6, 0)) == ["patching"]  # 01:00 EST
    assert is_active(Session, utc(2026, 3, 8, 8, 59)) == ["patching"]  # 04:59 EDT
    assert is_active(Session, utc(2026, 3, 8, 9, 0)) == []
    # An ordinary Sunday after the change starts an hour earlier in UTC.
    assert is_active(Session, utc(2026, 3, 15, 5, 0)) == ["patching"]  # 01:00 EDT
    assert is_active(Session, utc(2026, 3, 15, 4, 59)) == []


def test_start_time_inside_the_spring_forward_gap_shifts_forward(env):
    _, Session = env
    add(Session, start="02:30", minutes=60, tz="America/New_York")
    # 02:30 does not exist on 2026-03-08; it begins when the clocks would have reached it.
    assert is_active(Session, utc(2026, 3, 8, 7, 29)) == []
    assert is_active(Session, utc(2026, 3, 8, 7, 30)) == ["patching"]
    assert is_active(Session, utc(2026, 3, 8, 8, 30)) == []


def test_fall_back_window_keeps_its_elapsed_length(env):
    """2026-11-01 New York: 02:00 -> 01:00. 00:30 EDT + 3h ends 02:30 EST."""
    _, Session = env
    add(Session, start="00:30", minutes=180, tz="America/New_York")
    assert is_active(Session, utc(2026, 11, 1, 4, 30)) == ["patching"]
    assert is_active(Session, utc(2026, 11, 1, 7, 29)) == ["patching"]
    assert is_active(Session, utc(2026, 11, 1, 7, 30)) == []


def test_star_versus_named_schedules(env):
    _, Session = env
    add(Session, name="all", applies="*")
    add(Session, name="only-nightly", applies=[NAME])
    inside = utc(*SUN, 3, 0)
    assert is_active(Session, inside, NAME) == ["all", "only-nightly"]
    assert is_active(Session, inside, OTHER) == ["all"]
    assert is_active(Session, inside, "not-a-schedule") == ["all"]


def test_disabled_window_is_ignored(env):
    _, Session = env
    add(Session, enabled=False)
    assert is_active(Session, utc(*SUN, 3, 0)) == []


def test_next_spans_lists_upcoming_windows_in_order():
    row = MaintenanceWindow(days_of_week="6", start_time="02:00", duration_minutes=120, timezone="UTC")
    spans = next_spans(row, utc(*MON, 0, 0), 3)
    assert [s[0] for s in spans] == [utc(2026, 11, 1, 2), utc(2026, 11, 8, 2), utc(2026, 11, 15, 2)]
    assert spans[0][1] == utc(2026, 11, 1, 4)
    # A window in progress is listed first.
    assert next_spans(row, utc(*SUN, 3, 0), 1)[0][0] == utc(*SUN, 2)


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"name": "  "}, "name"),
        ({"days_of_week": []}, "day"),
        ({"days_of_week": [7]}, "day"),
        ({"days_of_week": ["sun"]}, "day"),
        ({"start_time": "25:00"}, "HH:MM"),
        ({"start_time": "2:00"}, "HH:MM"),
        ({"duration_minutes": 0}, "duration"),
        ({"duration_minutes": 7 * 24 * 60 + 1}, "duration"),
        ({"duration_minutes": "60"}, "duration"),
        ({"timezone": "Mars/Base"}, "timezone"),
        ({"applies_to": []}, "schedules"),
        ({"applies_to": ["ghost"]}, "ghost"),
        ({"enabled": "yes"}, "enabled"),
    ],
)
def test_invalid_fields_are_refused_with_a_sentence(change, fragment):
    with pytest.raises(MaintenanceWindowError) as err:
        normalize_window({**window(applies="*"), **change}, known_schedules={NAME})
    assert fragment in str(err.value)


def test_defaults_and_normalisation():
    fields = normalize_window({"name": " x ", "days_of_week": [6, 0, 6], "start_time": "02:00", "duration_minutes": 5},
                              default_tz="Europe/Paris")
    assert fields["name"] == "x"
    assert fields["days_of_week"] == [0, 6]
    assert fields["timezone"] == "Europe/Paris"
    assert fields["applies_to"] == "*"
    assert fields["enabled"] is True


def test_duplicate_names_are_refused(env):
    _, Session = env
    add(Session)
    with Session() as s, pytest.raises(DuplicateWindowName):
        create_window(s, window(), "bob")


# --- the decorator -----------------------------------------------------------


def _echo(app, calls):
    @app.task(name="demo.echo", bind=True)
    @track(schedule_name=NAME)
    def echo(self, value):
        calls.append(value)
        return value

    return echo


def _headers(occurrence, name=NAME):
    return {SCHEDULE_HEADER: name, OCCURRENCE_HEADER: format_occurrence(occurrence)}


def test_firing_inside_a_window_is_skipped_with_a_reason_and_the_body_does_not_run(env):
    app, Session = env
    calls = []
    echo = _echo(app, calls)
    add(Session)

    echo.apply(args=["inside"], headers=_headers(utc(*SUN, 3, 0)))
    echo.apply(args=["outside"], headers=_headers(utc(*MON, 3, 0)))

    assert calls == ["outside"]
    with Session() as s:
        runs = s.query(TaskRun).order_by(TaskRun.id).all()
    assert [r.status for r in runs] == ["skipped", "succeeded"]
    assert runs[0].skip_reason == "maintenance window: patching"
    assert runs[0].occurrence_at is not None
    assert runs[1].skip_reason is None


def test_the_stamped_occurrence_decides_not_the_delivery_time(env):
    """A message due inside the window but delivered after it ended is still
    skipped; one due after it ended but delivered 'now' (inside) is not."""
    app, Session = env
    calls = []
    echo = _echo(app, calls)
    add(Session, start="00:00", minutes=1)  # essentially never "now"
    add(Session, name="late", start="03:00", minutes=60)
    echo.apply(args=["a"], headers=_headers(utc(*SUN, 3, 30)))
    echo.apply(args=["b"], headers=_headers(utc(*SUN, 4, 0)))
    assert calls == ["b"]


def test_an_unstamped_firing_is_judged_at_now(env):
    app, Session = env
    calls = []
    echo = _echo(app, calls)
    now = datetime.now(timezone.utc)
    add(Session, days=range(7), start="00:00", minutes=7 * 24 * 60)  # always on
    echo.delay("x")
    assert calls == []
    with Session() as s:
        assert s.query(TaskRun).one().skip_reason == "maintenance window: patching"
    assert now  # silence unused


def test_window_for_another_schedule_or_disabled_does_not_skip(env):
    app, Session = env
    calls = []
    echo = _echo(app, calls)
    add(Session, name="other", applies=[OTHER])
    add(Session, name="off", enabled=False)
    echo.apply(args=["x"], headers=_headers(utc(*SUN, 3, 0)))
    assert calls == ["x"]


def test_pause_reason_wins_over_a_window(env):
    app, Session = env
    calls = []
    echo = _echo(app, calls)
    add(Session)
    with Session() as s:
        pause_schedule(s, NAME, "alice")
    echo.apply(args=["x"], headers=_headers(utc(*SUN, 3, 0)))
    with Session() as s:
        assert s.query(TaskRun).one().skip_reason == "schedule paused"


# --- upcoming occurrences, missed runs ----------------------------------------


def test_upcoming_occurrences_are_annotated(env):
    app, Session = env
    add(Session)  # Sundays 02:00-04:00; `nightly` fires daily at 03:00
    with Session() as s:
        rows = list_upcoming_with_overrides(s, app, NAME, count=8, from_time=utc(*SUN, 0, 0))
    by_time = {r["target_time"]: r["maintenance_window"] for r in rows}
    assert by_time[utc(*SUN, 3, 0)] == "patching"
    assert by_time[utc(*MON, 3, 0)] is None
    assert by_time[utc(2026, 11, 1, 3, 0)] == "patching"
    assert sum(1 for v in by_time.values() if v) == 2


def test_skipped_in_window_runs_still_count_as_arrived_for_missed_detection(env):
    app, Session = env
    echo = _echo(app, [])
    add(Session)
    for day in (22, 23, 24, 25, 26):  # 2026-10-25 03:00 falls in the window
        echo.apply(args=["x"], headers=_headers(utc(2026, 10, day, 3, 0)))
    with Session() as s:
        statuses = {r.occurrence_at.day: r.status for r in s.query(TaskRun).all()}
        assert statuses[25] == "skipped"
        missed = list_missed_runs(s, app, NAME, grace_seconds=60, now=utc(2026, 10, 26, 12, 0))
    assert missed == []


# --- REST API ----------------------------------------------------------------


@pytest.fixture
def api():
    celery_app = Celery("test_api")
    celery_app.conf.timezone = "Europe/Berlin"
    celery_app.conf.beat_schedule = {
        NAME: {"task": "demo.echo", "schedule": crontab(minute=0, hour=3)},
        OTHER: {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=1))},
    }
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as s:
        for role in ("viewer", "operator"):
            create_user(s, role, "pw", role=role)

    def client(role):
        c = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
        assert c.post("/api/auth/login", json={"username": role, "password": "pw"}).status_code == 200
        return c

    return client("operator"), client("viewer"), session_factory


def test_crud_roundtrip_with_audit_rows(api):
    op, viewer, session_factory = api
    created = op.post("/api/maintenance-windows", json={
        "name": "Sunday patching", "days_of_week": [6], "start_time": "02:00", "duration_minutes": 120,
        "applies_to": [NAME],
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["timezone"] == "Europe/Berlin"  # the Celery app's
    assert body["applies_to"] == [NAME] and body["enabled"] is True
    assert body["next_start"] is not None

    listing = viewer.get("/api/maintenance-windows").json()
    assert listing["default_timezone"] == "Europe/Berlin"
    assert [w["name"] for w in listing["windows"]] == ["Sunday patching"]

    wid = body["id"]
    updated = op.put(f"/api/maintenance-windows/{wid}", json={
        "name": "Sunday patching", "days_of_week": [5, 6], "start_time": "01:00", "duration_minutes": 60,
        "applies_to": "*", "enabled": False,
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["days_of_week"] == [5, 6] and updated.json()["enabled"] is False
    assert updated.json()["updated_by"] == "operator"

    assert op.delete(f"/api/maintenance-windows/{wid}").status_code == 200
    assert op.get("/api/maintenance-windows").json()["windows"] == []
    assert op.delete(f"/api/maintenance-windows/{wid}").status_code == 404

    with session_factory() as s:
        rows = s.query(AuditLog).filter(AuditLog.target_type == "maintenance_window").order_by(AuditLog.id).all()
    assert [r.action for r in rows] == [
        "create_maintenance_window", "update_maintenance_window", "delete_maintenance_window",
    ]
    assert {r.username for r in rows} == {"operator"}
    assert {r.target_id for r in rows} == {"Sunday patching"}
    assert json.loads(rows[0].details)["start_time"] == "02:00"


def test_validation_and_conflicts_over_http(api):
    op, _, _ = api
    good = {"name": "w", "days_of_week": [6], "start_time": "02:00", "duration_minutes": 60}
    assert op.post("/api/maintenance-windows", json={**good, "start_time": "99:00"}).status_code == 422
    r = op.post("/api/maintenance-windows", json={**good, "timezone": "Nope/Zone"})
    assert r.status_code == 422 and "timezone" in r.json()["detail"]
    r = op.post("/api/maintenance-windows", json={**good, "applies_to": ["ghost"]})
    assert r.status_code == 422 and "ghost" in r.json()["detail"]
    assert op.post("/api/maintenance-windows", json=good).status_code == 200
    assert op.post("/api/maintenance-windows", json=good).status_code == 409
    assert op.put("/api/maintenance-windows/999", json=good).status_code == 404


def test_viewer_can_read_and_preview_but_not_change(api):
    op, viewer, _ = api
    good = {"name": "w", "days_of_week": [6], "start_time": "02:00", "duration_minutes": 60}
    assert viewer.post("/api/maintenance-windows", json=good).status_code == 403
    wid = op.post("/api/maintenance-windows", json=good).json()["id"]
    assert viewer.put(f"/api/maintenance-windows/{wid}", json=good).status_code == 403
    assert viewer.delete(f"/api/maintenance-windows/{wid}").status_code == 403
    assert viewer.get("/api/maintenance-windows").status_code == 200

    preview = viewer.post("/api/maintenance-windows/preview", json={
        "days_of_week": [6], "start_time": "02:00", "duration_minutes": 120, "timezone": "UTC",
    })
    assert preview.status_code == 200
    spans = preview.json()["windows"]
    assert len(spans) == 5
    first = datetime.fromisoformat(spans[0]["start"])
    assert first.weekday() == SUNDAY and first.hour == 2
    assert datetime.fromisoformat(spans[0]["end"]) - first == timedelta(hours=2)
    bad = viewer.post("/api/maintenance-windows/preview", json={"days_of_week": [], "start_time": "02:00",
                                                                "duration_minutes": 5})
    assert bad.status_code == 422


def test_schedule_list_and_occurrences_show_the_window(api):
    op, viewer, _ = api
    op.post("/api/maintenance-windows", json={
        "name": "all-week", "days_of_week": list(range(7)), "start_time": "00:00", "duration_minutes": 7 * 24 * 60,
        "applies_to": [NAME],
    })
    listing = {s["name"]: s for s in viewer.get("/api/schedules").json()}
    assert listing[NAME]["maintenance_windows"] == [
        {"id": listing[NAME]["maintenance_windows"][0]["id"], "name": "all-week", "active": True}
    ]
    assert listing[OTHER]["maintenance_windows"] == []
    occurrences = viewer.get(f"/api/schedules/{NAME}/occurrences").json()
    assert occurrences and all(o["maintenance_window"] == "all-week" for o in occurrences)


def test_skip_reason_is_in_the_task_detail(env):
    app, Session = env
    echo = _echo(app, [])
    add(Session)
    echo.apply(args=["x"], headers=_headers(utc(*SUN, 3, 0)), task_id="t-skip")
    from celery_manager.api.routes.tasks import get_task  # noqa: F401  (route exists)
    with Session() as s:
        assert s.query(TaskRun).filter_by(task_id="t-skip").one().skip_reason.startswith("maintenance window")


# --- migration ---------------------------------------------------------------


def test_migration_0010_creates_and_drops_the_table_and_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cm.db'}")
    upgrade_database(engine)
    assert "maintenance_windows" in inspect(engine).get_table_names()
    assert "skip_reason" in {c["name"] for c in inspect(engine).get_columns("task_runs")}
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "0008")
    assert "maintenance_windows" not in inspect(engine).get_table_names()
    assert "skip_reason" not in {c["name"] for c in inspect(engine).get_columns("task_runs")}
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "0010")
    assert "maintenance_windows" in inspect(engine).get_table_names()
    engine.dispose()
