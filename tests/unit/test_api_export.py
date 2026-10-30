import csv
import io
import json
from datetime import datetime, timezone

from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import AuditLog, TaskRun


def _client(max_rows=50_000, runs=None):
    config = CeleryManagerConfig(
        celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret", export_max_rows=max_rows
    )
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        for run in runs if runs is not None else _default_runs():
            session.add(run)
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory


def _default_runs():
    return [
        TaskRun(task_id="a", task_name="mail.send", status="failed", args='["x"]', kwargs='{"to": "smtp"}',
                started_at=datetime(2026, 8, 10, 12, tzinfo=timezone.utc), exception="Boom", queue="mail", retry_count=2),
        TaskRun(task_id="b", task_name="mail.send", status="succeeded", args="[]", kwargs="{}",
                started_at=datetime(2026, 8, 12, 12, tzinfo=timezone.utc), runtime_seconds=1.5),
        TaskRun(task_id="c", task_name="img.resize", status="failed", args="[]", kwargs="{}"),
    ]


def test_export_requires_auth():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    assert TestClient(app, base_url="https://testserver").get("/api/tasks/export").status_code == 401


def test_export_csv_columns_and_headers():
    client, _ = _client()
    response = client.get("/api/tasks/export?format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="task-runs-') and disposition.endswith('.csv"')
    assert response.headers["x-export-truncated"] == "false"
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert list(rows[0].keys()) == [
        "task_id", "task_name", "status", "queue", "worker", "args", "kwargs",
        "started_at", "finished_at", "runtime_seconds", "retry_count", "exception", "schedule_name", "source", "tenant_id", "operation_key", "correlation_id",
    ]
    assert [r["task_id"] for r in rows] == ["c", "b", "a"]  # newest first
    assert rows[2]["retry_count"] == "2" and rows[2]["exception"] == "Boom"


def test_export_json_is_valid_array_with_decoded_args():
    client, _ = _client()
    response = client.get("/api/tasks/export?format=json")
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"].endswith('.json"')
    body = json.loads(response.text)
    assert [r["task_id"] for r in body] == ["c", "b", "a"]
    assert body[2]["args"] == ["x"] and body[2]["kwargs"] == {"to": "smtp"}


def test_export_json_empty_is_valid():
    client, _ = _client(runs=[])
    assert json.loads(client.get("/api/tasks/export?format=json").text) == []


def test_export_rejects_unknown_format():
    client, _ = _client()
    assert client.get("/api/tasks/export?format=xml").status_code == 422


def test_export_filter_parity_with_list_endpoint():
    client, _ = _client()
    for qs in [
        "status=failed",
        "task_name=mail.send",
        "q=smtp",
        "date_from=2026-08-11T00:00:00Z",
        "date_to=2026-08-11T00:00:00Z",
        "status=failed&task_name=mail.send",
    ]:
        listed = client.get(f"/api/tasks?{qs}").json()["items"]
        exported = json.loads(client.get(f"/api/tasks/export?format=json&{qs}").text)
        assert [r["task_id"] for r in exported] == [r["task_id"] for r in listed], qs


def test_export_cap_and_truncation_header():
    client, _ = _client(max_rows=2)
    response = client.get("/api/tasks/export?format=json")
    assert response.headers["x-export-truncated"] == "true"
    assert response.headers["x-export-total"] == "3"
    assert [r["task_id"] for r in json.loads(response.text)] == ["c", "b"]


def test_export_csv_formula_injection_is_escaped():
    runs = [
        TaskRun(task_id="x", task_name="=HYPERLINK(\"http://evil\")", status="failed", args="[]", kwargs="{}",
                exception="@SUM(1)", worker="+cmd", queue="-1"),
    ]
    client, _ = _client(runs=runs)
    row = next(csv.DictReader(io.StringIO(client.get("/api/tasks/export").text)))
    assert row["task_name"] == "'=HYPERLINK(\"http://evil\")"
    assert row["exception"] == "'@SUM(1)"
    assert row["worker"] == "'+cmd"
    assert row["queue"] == "'-1"
    assert row["args"] == "[]"  # untouched


def test_export_writes_audit_row_with_filter_and_count():
    client, session_factory = _client()
    client.get("/api/tasks/export?format=csv&status=failed")
    with session_factory() as session:
        row = session.query(AuditLog).filter_by(action="export_tasks").one()
        details = json.loads(row.details)
    assert row.username == "alice"
    assert details["rows"] == 2 and details["format"] == "csv"
    assert details["filters"] == {"status": "failed"}
    assert details["truncated"] is False
