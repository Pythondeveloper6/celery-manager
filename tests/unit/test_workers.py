import json
from unittest.mock import MagicMock

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.workers import (
    WorkerControlError,
    inspect_workers,
    is_valid_rate_limit,
    list_task_rate_limits,
)
from celery_manager.db.models import AuditLog

W1 = "celery@alpha"
W2 = "celery@beta"


def _inspector(**replies):
    """A stand-in for `control.inspect(...)` whose methods return the given
    replies (None when not given, which is what Celery returns when no
    worker answered)."""
    insp = MagicMock()
    for method in ("ping", "stats", "active", "reserved", "scheduled", "active_queues"):
        getattr(insp, method).return_value = replies.get(method)
    return insp


def _app_with(insp):
    app = MagicMock(spec=Celery)
    app.control.inspect.return_value = insp
    return app


# --- control layer: inspect_workers ---------------------------------------


def test_inspect_workers_merges_every_reply_per_worker():
    insp = _inspector(
        ping={W1: {"ok": "pong"}},
        stats={W1: {
            "pid": 4242,
            "uptime": 360,
            "pool": {"implementation": "celery.concurrency.prefork:TaskPool", "max-concurrency": 4, "processes": [1, 2, 3, 4]},
            "total": {"demo.add": 7, "demo.echo": 3},
        }},
        active={W1: [{"id": "t-1", "name": "demo.add", "time_start": 1.5}]},
        reserved={W1: [{"id": "t-2", "name": "demo.echo"}]},
        scheduled={W1: [{"eta": "2026-10-26T10:00:00", "request": {"id": "t-3", "name": "demo.add"}}]},
        active_queues={W1: [{"name": "default"}, {"name": "emails"}]},
    )
    [w] = inspect_workers(_app_with(insp), timeout=0.5)

    assert w["name"] == W1
    assert w["online"] is True
    assert w["pid"] == 4242
    assert w["uptime_seconds"] == 360
    assert w["pool"] == "prefork"
    assert w["concurrency"] == 4
    assert w["pool_processes"] == 4
    assert w["processed_total"] == 10
    assert (w["active_count"], w["reserved_count"], w["scheduled_count"]) == (1, 1, 1)
    assert w["active"] == [{"id": "t-1", "name": "demo.add", "time_start": 1.5}]
    assert w["scheduled"][0]["id"] == "t-3"
    assert w["scheduled"][0]["eta"] == "2026-10-26T10:00:00"
    assert w["queues"] == ["default", "emails"]


def test_inspect_workers_addresses_follow_ups_to_the_workers_that_pinged():
    insp = _inspector(ping={W2: {"ok": "pong"}, W1: {"ok": "pong"}})
    app = _app_with(insp)
    inspect_workers(app, timeout=0.5)
    first, second = app.control.inspect.call_args_list
    assert first.kwargs == {"timeout": 0.5}
    # limit lets the follow-ups return as soon as everyone answered instead
    # of each waiting out the full timeout.
    assert second.kwargs == {"destination": [W1, W2], "timeout": 0.5, "limit": 2}


def test_inspect_workers_with_no_reply_returns_empty_list_after_one_call():
    insp = _inspector()  # every method returns None
    app = _app_with(insp)
    assert inspect_workers(app, timeout=0.5) == []
    assert app.control.inspect.call_count == 1
    insp.stats.assert_not_called()


def test_inspect_workers_keeps_a_worker_that_missed_the_follow_ups():
    insp = _inspector(ping={W1: {"ok": "pong"}})
    [w] = inspect_workers(_app_with(insp), timeout=0.5)
    assert w["online"] is True
    assert w["pid"] is None
    assert w["queues"] == []
    assert w["active_count"] == 0


def test_inspect_workers_wraps_broker_errors():
    insp = MagicMock()
    insp.ping.side_effect = OperationalError("Error 61 connecting to localhost:6379. Connection refused.")
    with pytest.raises(WorkerControlError, match="Couldn't reach the Celery broker"):
        inspect_workers(_app_with(insp), timeout=0.5)


# --- rate-limit validation and registry listing ---------------------------


@pytest.mark.parametrize("value", ["", "10", "10/s", "10/m", "100/h", "2.5/m"])
def test_valid_rate_limits(value):
    assert is_valid_rate_limit(value)


@pytest.mark.parametrize("value", ["10/d", "ten/m", "/m", "-1/m", "10 /m", "10/minute", "1e3", "10/m;"])
def test_invalid_rate_limits(value):
    assert not is_valid_rate_limit(value)


def test_list_task_rate_limits_excludes_celery_internals():
    app = Celery("t")

    @app.task(name="demo.limited", rate_limit="10/m")
    def limited():
        pass

    @app.task(name="demo.free")
    def free():
        pass

    rows = list_task_rate_limits(app)
    # `app.task` defaults to shared=True, so other tests' tasks land in every
    # app: look only at this test's own (and their ordering).
    mine = [r for r in rows if r["task_name"] in ("demo.free", "demo.limited")]
    assert mine == [
        {"task_name": "demo.free", "rate_limit": None},
        {"task_name": "demo.limited", "rate_limit": "10/m"},
    ]
    assert not any(r["task_name"].startswith("celery.") for r in rows)


# --- HTTP layer -------------------------------------------------------------


def _client(celery_app=None):
    celery_app = celery_app or MagicMock(spec=Celery)
    config = CeleryManagerConfig(
        celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret",
        inspect_timeout_seconds=0.25,
    )
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory, celery_app


def _audit_rows(session_factory):
    with session_factory() as session:
        return [(r.action, r.target_type, r.target_id, json.loads(r.details) if r.details else None)
                for r in session.query(AuditLog).order_by(AuditLog.id)]


@pytest.mark.parametrize("method,path", [
    ("get", "/api/workers"),
    ("get", "/api/workers/rate-limits"),
    ("post", f"/api/workers/{W1}/shutdown"),
    ("post", f"/api/workers/{W1}/pool/grow"),
    ("post", f"/api/workers/{W1}/pool/shrink"),
    ("post", f"/api/workers/{W1}/queues/add"),
    ("post", f"/api/workers/{W1}/queues/cancel"),
    ("post", "/api/tasks/rate-limit"),
])
def test_worker_endpoints_require_auth(method, path):
    config = CeleryManagerConfig(celery_app=MagicMock(spec=Celery), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    response = getattr(TestClient(app, base_url="https://testserver"), method)(path)
    assert response.status_code == 401


def test_list_workers_endpoint_uses_configured_timeout():
    client, _, celery_app = _client()
    celery_app.control.inspect.return_value = _inspector(ping={W1: {"ok": "pong"}})
    response = client.get("/api/workers")
    assert response.status_code == 200
    body = response.json()
    assert [w["name"] for w in body["workers"]] == [W1]
    assert body["timeout_seconds"] == 0.25
    assert celery_app.control.inspect.call_args_list[0].kwargs == {"timeout": 0.25}


def test_list_workers_endpoint_empty_when_nobody_answers():
    client, _, celery_app = _client()
    celery_app.control.inspect.return_value = _inspector()
    response = client.get("/api/workers")
    assert response.status_code == 200
    assert response.json()["workers"] == []


def test_list_workers_endpoint_answers_503_on_broker_error():
    client, _, celery_app = _client()
    insp = MagicMock()
    insp.ping.side_effect = ConnectionRefusedError(61, "Connection refused")
    celery_app.control.inspect.return_value = insp
    response = client.get("/api/workers")
    assert response.status_code == 503
    assert "Couldn't reach the Celery broker" in response.json()["detail"]


def test_shutdown_endpoint_broadcasts_and_audits():
    client, sf, celery_app = _client()
    response = client.post(f"/api/workers/{W1}/shutdown")
    assert response.status_code == 200
    celery_app.control.shutdown.assert_called_once_with(destination=[W1])
    assert _audit_rows(sf) == [("worker_shutdown", "worker", W1, None)]


def test_shutdown_endpoint_does_not_audit_when_broker_fails():
    client, sf, celery_app = _client()
    celery_app.control.shutdown.side_effect = OperationalError("down")
    response = client.post(f"/api/workers/{W1}/shutdown")
    assert response.status_code == 503
    assert _audit_rows(sf) == []


def test_pool_grow_and_shrink_report_acknowledgements_and_audit():
    client, sf, celery_app = _client()
    celery_app.control.pool_grow.return_value = [{W1: {"ok": "pool will grow"}}]
    celery_app.control.pool_shrink.return_value = [{W1: {"error": "pool does not support shrinking"}}]

    grow = client.post(f"/api/workers/{W1}/pool/grow", json={"n": 2})
    shrink = client.post(f"/api/workers/{W1}/pool/shrink")

    assert grow.json() == {"acknowledged": [W1], "errors": {}}
    assert shrink.json() == {"acknowledged": [], "errors": {W1: "pool does not support shrinking"}}
    celery_app.control.pool_grow.assert_called_once_with(2, destination=[W1], reply=True, timeout=0.25, limit=1)
    celery_app.control.pool_shrink.assert_called_once_with(1, destination=[W1], reply=True, timeout=0.25, limit=1)
    actions = [(a, n["n"]) for a, _, _, n in _audit_rows(sf)]
    assert actions == [("worker_pool_grow", 2), ("worker_pool_shrink", 1)]


def test_pool_grow_rejects_out_of_range_n():
    client, _, _ = _client()
    assert client.post(f"/api/workers/{W1}/pool/grow", json={"n": 0}).status_code == 422


def test_add_and_cancel_consumer():
    client, sf, celery_app = _client()
    celery_app.control.add_consumer.return_value = [{W1: {"ok": "add consumer emails"}}]
    celery_app.control.cancel_consumer.return_value = []  # nobody answered in time

    add = client.post(f"/api/workers/{W1}/queues/add", json={"queue": "emails"})
    cancel = client.post(f"/api/workers/{W1}/queues/cancel", json={"queue": "emails"})

    assert add.json()["acknowledged"] == [W1]
    assert cancel.json() == {"acknowledged": [], "errors": {}}
    celery_app.control.add_consumer.assert_called_once_with("emails", destination=[W1], reply=True, timeout=0.25, limit=1)
    celery_app.control.cancel_consumer.assert_called_once_with("emails", destination=[W1], reply=True, timeout=0.25, limit=1)
    assert [r[0] for r in _audit_rows(sf)] == ["worker_add_consumer", "worker_cancel_consumer"]


def test_add_consumer_rejects_empty_queue():
    client, _, celery_app = _client()
    assert client.post(f"/api/workers/{W1}/queues/add", json={"queue": ""}).status_code == 422
    celery_app.control.add_consumer.assert_not_called()


def test_rate_limit_endpoint_broadcasts_to_all_workers_and_audits():
    client, sf, celery_app = _client()
    celery_app.control.rate_limit.return_value = [
        {W1: {"ok": "new rate limit set successfully"}},
        {W2: {"error": "unknown task"}},
    ]
    response = client.post("/api/tasks/rate-limit", json={"task_name": "demo.add", "rate_limit": "10/m"})

    assert response.status_code == 200
    assert response.json() == {"acknowledged": [W1], "errors": {W2: "unknown task"}}
    celery_app.control.rate_limit.assert_called_once_with("demo.add", "10/m", reply=True, timeout=0.25)
    [(action, target_type, target_id, details)] = _audit_rows(sf)
    assert (action, target_type, target_id) == ("rate_limit", "task", "demo.add")
    assert details["rate_limit"] == "10/m"
    assert details["destination"] is None


def test_rate_limit_endpoint_can_target_one_worker_and_clear():
    client, _, celery_app = _client()
    celery_app.control.rate_limit.return_value = [{W1: {"ok": "rate limit disabled successfully"}}]
    response = client.post(
        "/api/tasks/rate-limit", json={"task_name": "demo.add", "rate_limit": "", "destination": [W1]},
    )
    assert response.status_code == 200
    celery_app.control.rate_limit.assert_called_once_with(
        "demo.add", "", reply=True, timeout=0.25, destination=[W1], limit=1,
    )


def test_rate_limit_endpoint_rejects_bad_rate_without_broadcasting_or_auditing():
    client, sf, celery_app = _client()
    response = client.post("/api/tasks/rate-limit", json={"task_name": "demo.add", "rate_limit": "10/day"})
    assert response.status_code == 422
    assert "is not a rate limit" in response.json()["detail"]
    celery_app.control.rate_limit.assert_not_called()
    assert _audit_rows(sf) == []


def test_rate_limit_endpoint_does_not_shadow_task_routes():
    # The rate-limit route lives in the workers router; the tasks router's
    # `/{task_id}` routes must not swallow it (and vice versa).
    client, _, celery_app = _client()
    celery_app.control.rate_limit.return_value = []
    assert client.post("/api/tasks/rate-limit", json={"task_name": "x", "rate_limit": "1/s"}).status_code == 200
    assert client.get("/api/tasks/rate-limit").status_code == 404  # no such run


def test_rate_limits_listing_endpoint():
    app = Celery("t")

    @app.task(name="demo.limited_5s", rate_limit="5/s")
    def limited():
        pass

    client, _, _ = _client(app)
    response = client.get("/api/workers/rate-limits")
    assert response.status_code == 200
    assert {"task_name": "demo.limited_5s", "rate_limit": "5/s"} in response.json()
    assert all(not r["task_name"].startswith("celery.") for r in response.json())
