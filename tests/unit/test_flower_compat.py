"""Flower-shaped read endpoints over celery-manager's stored history."""
from datetime import datetime, timedelta, timezone

from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import TaskRun, WorkerState

T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def _client(**options):
    app, sessions = create_app(CeleryManagerConfig(celery_app=Celery("flower"), db_url="sqlite:///:memory:",
                                                   secret_key="test-secret", **options))
    with sessions() as session:
        create_user(session, "viewer", "pw", role="viewer")
        session.add_all([
            TaskRun(task_id="ok", task_name="reports.build", status="succeeded", worker="celery@a", queue="reports",
                    args="[1]", kwargs="{}", received_at=T0, started_at=T0 + timedelta(seconds=1),
                    finished_at=T0 + timedelta(seconds=3), runtime_seconds=2.0, result_repr="42", tenant_id="t1"),
            TaskRun(task_id="bad", task_name="billing.charge", status="failed", worker="celery@b", args="[]",
                    kwargs="{}", received_at=T0 + timedelta(minutes=5), started_at=T0 + timedelta(minutes=5),
                    finished_at=T0 + timedelta(minutes=6), exception="ValueError('x')", traceback="tb",
                    tenant_id="t2"),
            WorkerState(hostname="celery@a", state="online", last_seen_at=T0, active=1, processed=10),
            WorkerState(hostname="celery@b", state="offline", last_seen_at=T0),
        ])
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    return client


def _signed_in(**options):
    client = _client(**options)
    client.post("/api/auth/login", json={"username": "viewer", "password": "pw"})
    return client


def test_endpoints_require_sign_in():
    client = _client()
    for path in ("/api/flower/tasks", "/api/flower/task/info/ok", "/api/flower/workers"):
        assert client.get(path).status_code == 401, path


def test_tasks_are_keyed_by_id_with_flower_states_and_epoch_times():
    tasks = _signed_in().get("/api/flower/tasks").json()
    assert list(tasks) == ["bad", "ok"]
    ok = tasks["ok"]
    assert ok["state"] == "SUCCESS" and ok["name"] == "reports.build" and ok["worker"] == "celery@a"
    assert ok["received"] == T0.timestamp() and ok["succeeded"] == (T0 + timedelta(seconds=3)).timestamp()
    assert ok["failed"] is None and ok["runtime"] == 2.0 and ok["result"] == "42" and ok["routing_key"] == "reports"
    assert tasks["bad"]["state"] == "FAILURE" and tasks["bad"]["exception"] == "ValueError('x')"


def test_filters_sorting_and_paging():
    client = _signed_in()
    assert list(client.get("/api/flower/tasks", params={"state": "FAILURE"}).json()) == ["bad"]
    assert list(client.get("/api/flower/tasks", params={"workername": "celery@a"}).json()) == ["ok"]
    assert list(client.get("/api/flower/tasks", params={"taskname": "billing.charge"}).json()) == ["bad"]
    assert list(client.get("/api/flower/tasks", params={"sort_by": "received"}).json()) == ["ok", "bad"]
    assert list(client.get("/api/flower/tasks", params={"sort_by": "-received"}).json()) == ["bad", "ok"]
    since = (T0 + timedelta(minutes=1)).isoformat()
    assert list(client.get("/api/flower/tasks", params={"received_start": since}).json()) == ["bad"]
    assert list(client.get("/api/flower/tasks", params={"received_end": str(T0.timestamp())}).json()) == ["ok"]
    assert list(client.get("/api/flower/tasks", params={"limit": 1, "offset": 1}).json()) == ["ok"]


def test_bad_parameters_are_400_not_500():
    client = _signed_in()
    assert client.get("/api/flower/tasks", params={"state": "WHATEVER"}).status_code == 400
    assert client.get("/api/flower/tasks", params={"sort_by": "args"}).status_code == 400
    assert client.get("/api/flower/tasks", params={"received_start": "yesterday"}).status_code == 400


def test_task_info_and_unknown_task():
    client = _signed_in()
    assert client.get("/api/flower/task/info/bad").json()["traceback"] == "tb"
    assert client.get("/api/flower/task/info/missing").status_code == 404


def test_workers_status_and_detail_forms():
    client = _signed_in()
    assert client.get("/api/flower/workers", params={"status": "true"}).json() == {"celery@a": True, "celery@b": False}
    detail = client.get("/api/flower/workers").json()
    assert detail["celery@a"]["active"] == 1 and detail["celery@b"]["status"] is False


def test_versioned_alias_exists():
    assert _signed_in().get("/api/v1/flower/workers", params={"status": "true"}).status_code == 200


def test_tenant_mode_denies_these_global_views():
    client = _signed_in(tenant_resolver=lambda request, principal: "t1")
    assert client.get("/api/flower/tasks").status_code == 403
