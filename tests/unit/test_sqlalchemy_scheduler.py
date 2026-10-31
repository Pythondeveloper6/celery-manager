"""Real SQL schedule persistence and conservative reconciliation contracts."""
import importlib
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery
from celery.schedules import schedule
from sqlalchemy import select

pytest.importorskip("sqlalchemy_celery_beat")

from celery_manager.schedulers.base import ScheduleEntry
from celery_manager.schedulers.reconcile import (
    ReconcileConflict,
    apply_reconcile,
    export_backend,
    load_export,
    plan_reconcile,
)
from celery_manager.schedulers.sqlalchemy import SQLAlchemyBeatBackend


@pytest.fixture
def backend(tmp_path):
    app = Celery("sql-contract", broker="memory://")
    app.conf.update(timezone="UTC", beat_dburi=f"sqlite:///{tmp_path / 'beat.db'}")
    result = SQLAlchemyBeatBackend(app, create_tables=True)
    yield result
    result.close()


def entry(name="sample", seconds=60):
    return ScheduleEntry(name, "contract.task", schedule(seconds), [1], {"x": 2},
                         {"queue": "slow", "headers": {"custom": "yes"}, "expires": 30})


def test_sql_roundtrip_restart_and_atomic_invalid_update(backend):
    backend.create(entry())
    with pytest.raises(FileExistsError):
        backend.create(entry())
    assert backend.get("sample").options == entry().options
    backend.update("sample", enabled=False, schedule=schedule(120))
    with pytest.raises(ValueError):
        backend.update("sample", options={"expires": -1}, schedule=schedule(15))
    reopened = SQLAlchemyBeatBackend(backend.app)
    try:
        saved = reopened.get("sample")
        assert saved.enabled is False and saved.schedule.run_every.total_seconds() == 120
        assert saved.args == [1] and saved.kwargs == {"x": 2}
    finally:
        reopened.close()
    backend.delete("sample")
    assert backend.list() == []


def test_sql_scheduler_stamps_advances_once_and_persists(backend):
    app = backend.app
    @app.task(name="contract.task", shared=False)
    def task():
        return 1
    backend.create(entry("stamped"))
    module, name = backend.beat_scheduler_class().split(":")
    scheduler = getattr(importlib.import_module(module), name)(app=app, lazy=True)
    try:
        owned = scheduler.schedule["stamped"]
        with patch.object(task, "apply_async", return_value=MagicMock(id="sent")) as send:
            scheduler.apply_async(owned)
        assert send.call_args.kwargs["headers"]["cm_schedule"] == "stamped"
        assert send.call_args.kwargs["headers"]["cm_occurrence"]
        scheduler.sync()
        with backend.sessions() as session:
            row = session.scalar(select(backend.Model).where(backend.Model.name == "stamped"))
            assert row.total_run_count == 1 and row.last_run_at is not None
    finally:
        scheduler.close()


def test_reconcile_dry_run_adopt_export_and_updates_opt_in(backend):
    backend.create(entry("extra"))
    desired = [entry("adopted")]
    observe = plan_reconcile(backend, desired)
    assert all(not change["apply"] for change in observe["changes"])
    assert apply_reconcile(backend, observe)["applied"] == []
    plan = plan_reconcile(backend, desired, ownership="sql")
    assert apply_reconcile(backend, plan)["applied"] == ["adopted"]
    assert backend.get("extra") is not None
    document = export_backend(backend)
    assert [item.name for item in load_export(document, backend.app)] == ["adopted", "extra"]
    altered = [entry("adopted", 120)]
    blocked = plan_reconcile(backend, altered, ownership="code")
    assert next(c for c in blocked["changes"] if c["name"] == "adopted")["apply"] is False
    plan = plan_reconcile(backend, altered, ownership="code", allow_updates=True)
    assert apply_reconcile(backend, plan)["deleted"] == []
    assert backend.get("adopted").schedule.run_every.total_seconds() == 120
    assert backend.get("extra") is not None


def test_reconcile_aborts_before_write_on_drift_or_invalid_plan(backend):
    plan = plan_reconcile(backend, [entry()], ownership="code")
    backend.create(entry("concurrent"))
    with pytest.raises(ReconcileConflict):
        apply_reconcile(backend, plan)
    assert backend.get("sample") is None
    plan = plan_reconcile(backend, [entry(), entry("z")], ownership="code")
    plan["changes"][-1]["after"]["enabled"] = "bad"
    with pytest.raises((ValueError, TypeError)):
        apply_reconcile(backend, plan)
    assert backend.get("sample") is None


def test_sql_startup_does_not_overwrite_operator_edits(backend):
    backend.create(entry())
    backend.app.conf.beat_schedule = {"sample": {"task": "contract.task", "schedule": 999}}
    module, name = backend.beat_scheduler_class().split(":")
    scheduler = getattr(importlib.import_module(module), name)(app=backend.app)
    try:
        assert backend.get("sample").schedule.run_every.total_seconds() == 60
    finally:
        scheduler.close()


def test_sql_explicit_timezone_roundtrip(backend):
    from celery_manager.schedule_specs import spec_from_schedule, validate_spec
    _, timing = validate_spec({"type": "crontab", "hour": "9", "minute": "0",
                               "timezone": "America/New_York"}, app=backend.app)
    backend.create(ScheduleEntry("business", "contract.task", timing))
    saved = backend.get("business")
    assert str(saved.schedule.tz) == "America/New_York"
    assert spec_from_schedule(saved.schedule)["timezone"] == "America/New_York"
    assert load_export(export_backend(backend), backend.app)[0].schedule.tz.key == "America/New_York"


def test_sql_rejects_upstream_semantics_it_cannot_preserve(backend):
    backend.create(entry())
    with backend.sessions.begin() as session:
        row = session.scalar(select(backend.Model).where(backend.Model.name == "sample"))
        row.one_off = True
    with pytest.raises(ValueError, match="one_off/start_time"):
        backend.get("sample")


def test_reconcile_duplicate_changes_rejected_before_writes(backend):
    plan = plan_reconcile(backend, [entry()], ownership="sql")
    plan["changes"].append(plan["changes"][0])
    with pytest.raises(ValueError, match="duplicate"):
        apply_reconcile(backend, plan)
    assert backend.list() == []


def test_sql_absolute_expiry_keeps_instant_across_sqlite_roundtrip(backend):
    from dataclasses import replace
    from datetime import datetime, timezone
    value = replace(entry(), options={"expires": "2030-01-01T10:00:00+02:00"})
    backend.create(value)
    assert backend.get("sample").options["expires"] == datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
    backend.update("sample", options={"expires": "2030-01-01T08:00:00Z"})
    assert backend.get("sample").options["expires"] == datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone"):
        backend.update("sample", options={"expires": "2030-01-01T10:00:00"})


def test_reconcile_http_auth_audit_and_conflict(backend):
    from fastapi.testclient import TestClient

    from celery_manager import CeleryManagerConfig
    from celery_manager.api.app import create_app
    from celery_manager.api.routes.schedule_reconcile import router
    from celery_manager.auth.external import ExternalUser
    from celery_manager.db.models import AuditLog
    @backend.app.task(name="contract.task", shared=False)
    def task():
        return 1
    backend.app.conf.beat_schedule = {"sample": {"task": "contract.task", "schedule": 60}}
    config = CeleryManagerConfig(celery_app=backend.app, db_url="sqlite:///:memory:", secret_key="s",
                                scheduler_backend=backend,
                                authenticate_request=lambda r: ExternalUser("host", r.headers.get("x-role", "admin")))
    api, sessions = create_app(config)
    if not any(getattr(route, "path", "") == "/api/schedule-reconciliation/plan" for route in api.routes):
        api.include_router(router)
    client = TestClient(api, base_url="https://testserver")
    assert client.post("/api/schedule-reconciliation/plan", json={}, headers={"x-role": "viewer"}).status_code == 403
    plan = client.post("/api/schedule-reconciliation/plan", json={"ownership": "sql"}).json()
    with (patch("celery_manager.api.routes.schedule_reconcile.record", side_effect=RuntimeError("audit unavailable")),
          pytest.raises(RuntimeError, match="audit unavailable")):
        client.post("/api/schedule-reconciliation/apply", json=plan)
    assert backend.get("sample") is None
    response = client.post("/api/schedule-reconciliation/apply", json=plan)
    assert response.status_code == 200 and response.json()["applied"] == ["sample"]
    assert client.post("/api/schedule-reconciliation/apply", json=plan).status_code == 409
    assert client.get("/api/schedule-reconciliation/export", headers={"x-role": "viewer"}).status_code == 200
    with sessions() as session:
        assert session.query(AuditLog).filter_by(action="schedule.reconcile").count() == 1


def test_two_in_memory_backends_do_not_share_upstream_engine_cache():
    app = Celery("isolated", broker="memory://")
    app.conf.beat_dburi = "sqlite:///:memory:"
    first = SQLAlchemyBeatBackend(app, create_tables=True)
    second = SQLAlchemyBeatBackend(app, create_tables=True)
    try:
        first.create(entry())
        assert second.list() == []
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("seconds", [0.5, 1.5, 2_147_483_648])
def test_sql_rejects_nonportable_integer_interval(backend, seconds):
    from dataclasses import replace
    with pytest.raises(ValueError, match="whole seconds"):
        backend.create(replace(entry(), schedule=schedule(seconds)))
    assert backend.list() == []
