from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.external import ExternalUser
from celery_manager.db.models import AuditLog, OperationAnnotation
from celery_manager.doctor import doctor


def named_task():
    return None


def configured(tmp_path, **overrides):
    celery = Celery("guards", broker="memory://")
    celery.task(name="demo.task", shared=False)(named_task)
    config = CeleryManagerConfig(celery_app=celery, db_url="sqlite:///" + str(tmp_path / "guards.db"),
                                secret_key="guard-test-private", environment_name="production", operator_safeguards=True,
                                authenticate_request=lambda _: ExternalUser("operator", "admin", "subject"), **overrides)
    app, sessions = create_app(config)
    return config, sessions, TestClient(app, base_url="https://testserver")


def test_sensitive_action_requires_audited_reason_and_exact_environment(tmp_path):
    config, sessions, client = configured(tmp_path)
    with patch.object(config.celery_app, "send_task", return_value=MagicMock(id="guard-run")) as send:
        body = {"task_name": "demo.task", "args": [], "kwargs": {}}
        assert client.post("/api/tasks/start", json=body).status_code == 428
        assert client.post("/api/v1/tasks/start", json=body, headers={"x-cm-action-reason":"test", "x-cm-confirm-environment":"staging"}).status_code == 428
        send.assert_not_called()
        reason = "إعادة تشغيل بعد مراجعة 100%"
        response = client.post("/api/v1/tasks/start", json=body, headers={"x-cm-action-reason":quote(reason), "x-cm-confirm-environment":"production"})
        assert response.status_code == 200, response.text
        send.assert_called_once()
    with sessions() as session:
        accepted = session.query(AuditLog).filter_by(action="guardrail.accepted").one()
        import json
        assert json.loads(accepted.details)["reason"] == reason
        assert accepted.actor_subject == "subject"
        assert session.query(AuditLog).filter_by(action="guardrail.denied").count() == 2


def test_guard_audit_outage_blocks_dispatch_and_cors_allows_explicit_headers(tmp_path):
    config, _, client = configured(tmp_path, trusted_origins=("https://host.test",))
    response = client.options("/api/tasks/start", headers={"Origin":"https://host.test", "Access-Control-Request-Method":"POST", "Access-Control-Request-Headers":"X-CM-Action-Reason,X-CM-Confirm-Environment"})
    assert response.status_code == 200
    with patch("celery_manager.control.audit.record", side_effect=RuntimeError("unavailable")), patch.object(config.celery_app, "send_task") as send:
        response = client.post("/api/tasks/start", json={"task_name":"demo.task"}, headers={"x-cm-action-reason":"reviewed", "x-cm-confirm-environment":"production"})
        assert response.status_code == 503
        send.assert_not_called()


def test_doctor_is_read_only_and_redacts_storage_secrets(tmp_path):
    config, _, _ = configured(tmp_path)
    report = doctor(config)
    assert report["ok"]
    assert not any(check["name"] == "broker" for check in report["checks"])
    config.db_url = "unknown://secret-password@invalid/database"
    report = doctor(config)
    assert not report["ok"] and "secret-password" not in str(report)


def test_annotations_are_environment_scoped_and_audited(tmp_path):
    config, sessions, client = configured(tmp_path)
    response = client.post("/api/operations/annotations", json={"kind":"deployment", "label":"Release", "revision":"abc"})
    assert response.status_code == 200, response.text
    assert client.post("/api/operations/annotations", json={"kind":"maintenance", "label":"missing end"}).status_code == 422
    with sessions() as session:
        assert session.query(OperationAnnotation).one().environment == "production"
        assert session.query(AuditLog).filter_by(action="operation.annotate").one()
    config.environment_name = "staging"
    assert client.get("/api/operations/annotations").json()["items"] == []


def test_guard_requires_explicit_environment():
    with pytest.raises(ValueError):
        CeleryManagerConfig(celery_app=None, operator_safeguards=True)


def test_context_uses_latest_deployment_and_half_open_maintenance_window(tmp_path):
    from datetime import datetime, timezone
    _config, _sessions, client = configured(tmp_path)
    for payload in (
        {"kind": "deployment", "label": "previous", "starts_at": "2026-01-01T00:00:00Z"},
        {"kind": "deployment", "label": "current", "starts_at": "2026-01-02T00:00:00Z"},
        {"kind": "deployment", "label": "future", "starts_at": "2026-02-01T00:00:00Z"},
        {"kind": "maintenance", "label": "window", "starts_at": "2026-01-02T00:00:00Z", "ends_at": "2026-01-03T00:00:00Z"},
    ):
        assert client.post("/api/operations/annotations", json=payload).status_code == 200
    def context(at):
        response = client.get("/api/operations/context", params={"at": datetime.fromisoformat(at).replace(tzinfo=timezone.utc).isoformat()})
        assert response.status_code == 200
        return response.json()
    result = context("2026-01-02T00:00:00")
    assert result["deployment"]["label"] == "current"
    assert [row["label"] for row in result["maintenance"]] == ["window"]
    assert context("2026-01-03T00:00:00")["maintenance"] == []
    assert context("2025-12-31T00:00:00")["deployment"] is None
    _config.environment_name = "staging"
    assert context("2026-01-02T00:00:00")["deployment"] is None


def test_doctor_does_not_create_missing_sqlite_database(tmp_path):
    missing = tmp_path / "unprovisioned.db"
    config = CeleryManagerConfig(celery_app=None, db_url="sqlite:///" + str(missing), secret_key="private-test")
    report = doctor(config)
    assert not report["ok"]
    assert not missing.exists()


def test_doctor_bounds_repeated_hanging_checks_to_one_call():
    from threading import Event

    from celery_manager.doctor import _bounded
    config = CeleryManagerConfig(celery_app=None)
    release = Event()
    calls = []
    def hanging_read():
        calls.append(1)
        release.wait(5)
        return "completed"
    try:
        assert _bounded(config, "test", hanging_read, 0.01) == (False, "timeout")
        assert _bounded(config, "test", hanging_read, 0.01) == (False, "timeout")
        assert len(calls) == 1
    finally:
        release.set()


@pytest.mark.parametrize("starts,ends", [
    (None, "2099-01-02T00:00:00"),
    ("2099-01-01T00:00:00", "2099-01-02T00:00:00Z"),
    ("2099-01-01T02:00:00+02:00", "2099-01-02T00:00:00"),
])
def test_annotations_accept_naive_utc_and_offset_dates_consistently(tmp_path, starts, ends):
    _config, _sessions, client = configured(tmp_path)
    response = client.post("/api/operations/annotations", json={"kind": "maintenance", "label": "window", "starts_at": starts, "ends_at": ends})
    assert response.status_code == 200, response.text
    assert response.json()["ends_at"] == "2099-01-02T00:00:00+00:00"


@pytest.mark.parametrize("uri_options", ["uri=true", "uri=true&mode=rwc"])
def test_doctor_never_creates_missing_sqlite_uri_database(tmp_path, uri_options):
    missing = tmp_path / "unprovisioned-uri.db"
    config = CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///file:{missing}?{uri_options}", secret_key="private-test")
    assert doctor(config)["checks"][0]["state"] == "fail"
    assert not missing.exists()


def test_doctor_reads_existing_sqlite_uri_without_runtime_initialization(tmp_path):
    from celery_manager.db.migrate import upgrade_database
    from celery_manager.db.session import create_db_engine
    database = tmp_path / "provisioned.db"
    url = f"sqlite:///file:{database}?uri=true"
    engine = create_db_engine(url)
    try:
        upgrade_database(engine)
    finally:
        engine.dispose()
    config = CeleryManagerConfig(celery_app=None, db_url=url, secret_key="private-test")
    assert doctor(config)["checks"][0]["state"] == "pass"


def test_doctor_sqlite_memory_uri_does_not_attempt_a_disk_file():
    config = CeleryManagerConfig(celery_app=None, db_url="sqlite:///file:doctor-memory?mode=memory&cache=shared&uri=true", secret_key="private-test")
    check = doctor(config)["checks"][0]
    assert check["state"] == "fail"
    assert check["detail"] == "Storage reachable; run the explicit migrate command"


def test_annotation_unicode_roundtrip_and_portable_label_bounds(tmp_path):
    _, _, client = configured(tmp_path)
    value = {"kind": "deployment", "label": "😀" * 127 + "x", "revision": "إصدار جديد", "note": "خطة الصيانة\n🚀"}
    response = client.post("/api/operations/annotations", json=value)
    assert response.status_code == 200, response.text
    assert all(response.json()[key] == item for key, item in value.items())
    listed = client.get("/api/operations/annotations").json()["items"]
    assert listed[0]["note"] == value["note"]
    assert client.post("/api/operations/annotations", json={**value, "label": "😀" * 128}).status_code == 422
    assert client.post("/api/operations/annotations", json={**value, "revision": "😀" * 128}).status_code == 422
    assert len(client.get("/api/operations/annotations").json()["items"]) == 1
