"""Backend contract and HTTP behavior without requiring a running scheduler."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery
from celery.schedules import schedule
from fastapi.testclient import TestClient

from celery_manager import CeleryManagerConfig, track
from celery_manager.api.app import create_app
from celery_manager.auth.external import ExternalUser
from celery_manager.db.models import (
    AuditLog,
    PeriodicSchedule,
    ScheduleOverride,
    TaskRun,
)
from celery_manager.schedulers import ScheduleEntry, StaticBeatBackend


class MemoryBackend:
    name = "memory-test"
    writable = True
    def __init__(self):
        self.entries = {}
    def list(self):
        return list(self.entries.values())
    def get(self, name):
        return self.entries.get(name)
    def create(self, entry):
        if entry.name in self.entries:
            raise FileExistsError(entry.name)
        self.entries[entry.name] = entry
        return entry
    def update(self, name, **changes):
        self.entries[name] = replace(self.entries[name], **changes)
        return self.entries[name]
    def delete(self, name):
        del self.entries[name]
    def beat_scheduler_class(self):
        return "test:Scheduler"


@pytest.fixture
def env():
    app = Celery("backends", broker="memory://")
    @app.task(name="backend.task", shared=False)
    @track(schedule_from=lambda args, kwargs: kwargs.get("schedule_name"), consume_kwargs=("schedule_name",))
    def task(value=1):
        return value
    backend = MemoryBackend()
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s",
                                scheduler_backend=backend, schedule_sync_interval_seconds=0,
                                authenticate_request=lambda r: ExternalUser("host", r.headers.get("x-role", "admin"), "person-4"))
    api, sessions = create_app(config)
    return app, backend, config, sessions, TestClient(api, base_url="https://testserver"), task


def payload(name="morning"):
    return {"name": name, "task": "backend.task", "schedule": {"type": "interval", "seconds": 60},
            "args": [5], "kwargs": {}}


def test_backend_create_edit_delete_preserves_history_and_audits_diff(env):
    _, backend, _, sessions, client, _ = env
    assert client.post("/api/schedules", json=payload()).status_code == 200
    assert len(client.get("/api/schedules/morning/occurrences").json()) == 10
    assert client.patch("/api/schedules/morning", json={"schedule": {"type": "interval", "seconds": 120}}).status_code == 200
    assert backend.get("morning").schedule.run_every.total_seconds() == 120
    with sessions() as session:
        session.add(ScheduleOverride(schedule_name="morning", target_time=datetime.now(timezone.utc) + timedelta(minutes=5),
                                     action="skip", consumed=False, created_by="host"))
        session.commit()
    assert client.delete("/api/schedules/morning").status_code == 200
    with sessions() as session:
        assert session.query(PeriodicSchedule).filter_by(name="morning").one().enabled is False
        assert session.query(ScheduleOverride).count() == 1
        rows = session.query(AuditLog).filter(AuditLog.action.like("schedule.%")).all()
        assert [r.action for r in rows] == ["schedule.create", "schedule.update", "schedule.delete"]
        assert all(r.actor_subject == "person-4" for r in rows)
    assert client.get("/api/schedules/morning/occurrences").json() == []


def test_external_timing_works_in_existing_editor(env):
    _, backend, _, _, client, _ = env
    client.post("/api/schedules", json=payload())
    response = client.put("/api/schedules/morning/schedule", json={"type": "interval", "seconds": 180})
    assert response.status_code == 200
    assert backend.get("morning").schedule.run_every.total_seconds() == 180
    assert response.json()["schedule_editing"] is True


def test_runtime_changes_and_disabled_entries_are_discovered(env):
    _, backend, _, _, client, _ = env
    backend.create(ScheduleEntry("external", "backend.task", schedule(60), enabled=False, source=backend.name))
    items = client.get("/api/schedules").json()
    assert items[0]["name"] == "external" and items[0]["enabled"] is False
    assert client.post("/api/schedules/external/skip-next").status_code == 409


def test_read_only_backend_refuses_crud_but_previews_text():
    app = Celery("static")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s",
                                authenticate_request=lambda r: ExternalUser("host", "admin"))
    api, _ = create_app(config)
    client = TestClient(api, base_url="https://testserver")
    assert StaticBeatBackend(app).writable is False
    assert client.post("/api/schedules", json=payload()).status_code == 409
    preview = client.post("/api/schedules/preview", json={"type": "text", "text": "weekdays at 06:30"})
    assert preview.status_code == 200 and len(preview.json()["occurrences"]) == 10


def test_two_schedules_of_one_task_keep_independent_pause_identity(env):
    _, _, _, sessions, client, task = env
    client.post("/api/schedules", json=payload("morning"))
    client.post("/api/schedules", json=payload("evening"))
    assert client.post("/api/schedules/morning/pause").status_code == 200
    task.apply(kwargs={"schedule_name": "morning"}, task_id="morning-run")
    task.apply(kwargs={"schedule_name": "evening"}, task_id="evening-run")
    with sessions() as session:
        assert session.query(TaskRun).filter_by(task_id="morning-run").one().status == "skipped"
        assert session.query(TaskRun).filter_by(task_id="evening-run").one().status == "succeeded"


def test_run_now_is_allowlist_checked_and_skip_next_is_idempotent(env):
    app, _, config, sessions, client, _ = env
    client.post("/api/schedules", json=payload())
    config.startable_tasks = set()
    with patch.object(app, "send_task") as send:
        assert client.post("/api/schedules/morning/run-now").status_code == 403
        send.assert_not_called()
    config.startable_tasks = None
    with patch.object(app, "send_task", return_value=MagicMock(id="manual")) as send:
        assert client.post("/api/schedules/morning/run-now").status_code == 200
        assert send.call_args.kwargs["headers"] == {"cm_initiator": "person-4"}
    assert client.post("/api/schedules/morning/skip-next").status_code == 200
    assert client.post("/api/schedules/morning/skip-next").status_code == 200
    with sessions() as session:
        assert session.query(ScheduleOverride).count() == 1


@pytest.mark.parametrize("method, path, data", [("POST", "/api/schedules", payload()),
                                                ("PATCH", "/api/schedules/morning", {"enabled": False}),
                                                ("DELETE", "/api/schedules/morning", None)])
def test_viewer_cannot_change_backend(env, method, path, data):
    _, backend, _, _, client, _ = env
    response = client.request(method, path, json=data, headers={"x-role": "viewer"})
    assert response.status_code == 403
    assert not backend.entries


def test_backend_outage_does_not_disable_existing_rows(env):
    _, backend, _, sessions, client, _ = env
    client.post("/api/schedules", json=payload())
    with patch.object(backend, "list", side_effect=ConnectionError("offline")):
        assert client.get("/api/schedules").status_code == 503
    with sessions() as session:
        assert session.query(PeriodicSchedule).one().enabled is True


def test_run_now_preserves_delivery_routing_without_occurrence_headers(env):
    app, backend, _, _, client, _ = env
    backend.create(ScheduleEntry("routed", "backend.task", schedule(60), options={
        "queue": "reports", "exchange": "reports-exchange", "routing_key": "reports.priority", "priority": 7,
        "expires": 60, "headers": {"domain": "stats", "cm_occurrence": "old"},
    }, source=backend.name))
    with patch.object(app, "send_task", return_value=MagicMock(id="routed-run")) as send:
        assert client.post("/api/schedules/routed/run-now").status_code == 200
    assert send.call_args.kwargs["exchange"] == "reports-exchange"
    assert send.call_args.kwargs["routing_key"] == "reports.priority"
    assert send.call_args.kwargs["priority"] == 7
    assert send.call_args.kwargs["expires"] == 60
    assert send.call_args.kwargs["headers"] == {"domain": "stats", "cm_initiator": "person-4"}


def test_django_expiry_mapping_does_not_drop_absolute_or_relative_expiry():
    from celery_manager.schedulers.django import DjangoCeleryBeatBackend
    adapter = DjangoCeleryBeatBackend.__new__(DjangoCeleryBeatBackend)
    adapter.Entry = MagicMock()
    adapter.Entry._unpack_fields.side_effect = lambda **kw: {}
    timestamp = datetime.now(timezone.utc) + timedelta(hours=1)
    absolute = adapter._fields(ScheduleEntry("absolute", "task", schedule(60), options={"expires": timestamp.isoformat()}))
    assert absolute["expires"] == timestamp and absolute["expire_seconds"] is None
    relative = adapter._fields(ScheduleEntry("relative", "task", schedule(60), options={"expires": 120}))
    assert relative["expires"] is None and relative["expire_seconds"] == 120


def test_text_preview_explicit_timezone_survives_create_and_edit(env):
    _, backend, _, _, client, _ = env
    result = client.post("/api/schedules/preview", json={"type": "text", "text": "weekdays at 06:30", "timezone": "Europe/Berlin"})
    assert result.status_code == 200
    assert result.json()["timezone"] == "Europe/Berlin"
    spec = result.json()["schedule"]
    assert spec["timezone"] == "Europe/Berlin"
    value = {**payload(), "schedule": spec, "options": {"queue": "reports", "priority": 5}}
    assert client.post("/api/schedules", json=value).status_code == 200
    assert str(backend.get("morning").schedule._cm_timezone) == "Europe/Berlin"
    assert client.patch("/api/schedules/morning", json={"args": [9], "kwargs": {"x": 1}, "options": {"queue": "other"}}).status_code == 200
    saved = backend.get("morning")
    assert saved.args == [9] and saved.kwargs == {"x": 1} and saved.options == {"queue": "other"}
    assert client.post("/api/schedules/preview", json={"type": "text", "text": "weekdays at 06:30", "timezone": "not/a-zone"}).status_code == 422
    assert client.post("/api/schedules/preview", json={"type": "text", "text": "weekdays at 06:30", "timezone": []}).status_code == 422
