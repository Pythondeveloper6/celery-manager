"""Unit tests for the telemetry breaker, cooperative cancel, durable revokes,
attempt history, outbound-call recording, runtime snapshots, probes and
scoped API tokens."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from celery_manager import progress, telemetry
from celery_manager.api.app import create_app
from celery_manager.auth import api_tokens
from celery_manager.auth.users import create_user
from celery_manager.cancel import raise_if_cancelled
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import attempts, revokes, snapshots
from celery_manager.control.tasks import cancel_task
from celery_manager.db.external import ExternalCall
from celery_manager.db.models import Base, TaskRun
from celery_manager.db.run_control import RevokedTask, TaskRunAttempt
from celery_manager.decorator import configure, track
from celery_manager.events.listener import upsert_task_run


@pytest.fixture(autouse=True)
def _fresh_breakers():
    # Breakers are keyed by id(config); a recycled id must not inherit an open breaker.
    telemetry._breakers.clear()
    yield
    telemetry._breakers.clear()


def _eager_app(name):
    app = Celery(name, broker="memory://", backend="cache+memory://",
                 task_cls="celery_manager.task:TrackedTask")
    app.conf.task_always_eager = True
    return app


def _sqlite_sessions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# --- 1. telemetry breaker -------------------------------------------------

def test_breaker_opens_after_failures_probes_once_and_closes_on_success():
    clock = FakeClock()
    breaker = telemetry.Breaker(failures=3, seconds=30, clock=clock)
    for _ in range(3):
        assert breaker.allow()
        breaker.record(False)
    assert breaker.open
    assert breaker.allow() is False and breaker.skipped == 1
    clock.now = 29.9
    assert breaker.allow() is False
    clock.now = 30.0
    assert breaker.allow() is True  # the probe
    assert breaker.allow() is False  # one probe per period
    breaker.record(True)
    assert not breaker.open and breaker.allow()


# --- 2. timeout options ---------------------------------------------------

def test_timeout_options_per_driver():
    assert telemetry.timeout_options("sqlite:///x.db", None) == {}
    sqlite = telemetry.timeout_options("sqlite:///x.db", 5)
    assert sqlite == {"pool_timeout": 5, "connect_args": {"timeout": 5}}
    pg = telemetry.timeout_options("postgresql+psycopg://u@db/app", 5)
    assert pg["pool_timeout"] == 5
    assert "statement_timeout=5000" in pg["connect_args"]["options"]


# --- 3 and 4. decorator behind a failing database -------------------------

def _broken_sessions():
    raise RuntimeError("monitoring database unreachable")


def test_tracked_task_still_returns_result_and_breaker_opens_after_three_runs():
    app = _eager_app("breaker-runs")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:")
    configure(_broken_sessions, config=config)

    @app.task(name="shop.tasks.sync_orders", bind=True)
    @track()
    def sync_orders(self, value):
        return value * 2

    for n in range(3):
        assert sync_orders.apply(args=[n], task_id=f"breaker-run-{n}").result == n * 2
    assert telemetry.breaker_for(config).open


def test_control_check_skip_means_body_does_not_run_when_sessions_fail():
    app = _eager_app("breaker-skip")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", control_check_on_failure="skip")
    configure(_broken_sessions, config=config)
    ran = []

    @app.task(name="shop.tasks.sync_inventory", bind=True)
    @track()
    def sync_inventory(self):
        ran.append(True)
        return "done"

    assert sync_inventory.apply(task_id="skip-1").result is None
    assert ran == []


# --- 5 and 6. cooperative cancel ------------------------------------------

def test_cooperative_cancel_stops_a_tracked_task_at_its_next_check():
    sessions = _sqlite_sessions()
    app = _eager_app("cancel-coop")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", cancel_check_interval_seconds=0)
    configure(sessions, config=config)
    with sessions() as session:
        session.add(TaskRun(task_id="coop-1", task_name="shop.tasks.sync_orders", status="started",
                            args="[]", kwargs="{}"))
        session.commit()
        cancel_task(None, "coop-1", session=session, username="ops", mode="cooperative")
    with sessions() as session:
        row = session.query(TaskRun).filter_by(task_id="coop-1").one()
        assert row.cancel_requested_at is not None and row.cancel_requested_by == "ops"

    events, finalized = [], []

    @app.task(name="shop.tasks.sync_orders", bind=True)
    @track()
    def sync_orders(self):
        try:
            for i in range(10):
                events.append(progress.report_progress(i, 10))  # True once cancelled
                raise_if_cancelled()
        finally:
            finalized.append(True)

    sync_orders.apply(task_id="coop-1")
    assert events == [True]
    assert finalized == [True]
    with sessions() as session:
        assert session.query(TaskRun).filter_by(task_id="coop-1").one().status == "cancelled"


def test_cancel_task_rejects_bad_mode_and_signal_on_non_terminate():
    with pytest.raises(ValueError):
        cancel_task(None, "x-1", mode="bogus")
    with pytest.raises(ValueError):
        cancel_task(None, "x-1", mode="revoke", signal="SIGTERM")


# --- 7. durable revokes ----------------------------------------------------

def test_durable_revokes_store_expire_check_and_resend():
    sessions = _sqlite_sessions()
    now = datetime.now(timezone.utc)
    eta = now + timedelta(hours=100)
    with sessions() as session:
        session.add(TaskRun(task_id="rev-eta", task_name="shop.tasks.sync_orders", status="queued", eta=eta))
        session.commit()
        revokes.remember_revokes(session, ["rev-1", "rev-eta"], username="ops", now=now)
        session.commit()
        # SQLite returns naive datetimes; the stored values are UTC.
        rows = {r.task_id: r.expires_at.replace(tzinfo=timezone.utc) for r in session.query(RevokedTask).all()}
        assert rows["rev-1"] >= now + timedelta(hours=48)
        assert rows["rev-eta"] >= eta
        assert revokes.is_revoked(session, "rev-1", now=now)
        assert not revokes.is_revoked(session, "never-revoked", now=now)

        revokes.remember_revokes(session, ["old-1"], username="ops", retention_hours=1,
                                 now=now - timedelta(hours=100))
        session.commit()
        assert revokes.prune_revokes(session, now=now) == 1
        assert session.get(RevokedTask, "old-1") is None

        app = MagicMock()
        assert revokes.resend_revokes(app, session, "worker@host", now=now) == 2
        app.control.revoke.assert_called_once()
        assert app.control.revoke.call_args.kwargs["destination"] == ["worker@host"]


def test_tracked_task_with_stored_revoke_does_not_run_and_is_recorded_revoked():
    sessions = _sqlite_sessions()
    app = _eager_app("revoked-run")
    configure(sessions, config=CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:"))
    with sessions() as session:
        revokes.remember_revokes(session, ["rev-run-1"], username="ops")
        session.commit()
    ran = []

    @app.task(name="shop.tasks.sync_orders", bind=True)
    @track()
    def sync_orders(self):
        ran.append(True)

    sync_orders.apply(task_id="rev-run-1")
    assert ran == []
    with sessions() as session:
        assert session.query(TaskRun).filter_by(task_id="rev-run-1").one().status == "revoked"


# --- 8. attempt history ----------------------------------------------------

def test_upsert_attempt_merges_without_overwriting_and_is_unique_per_attempt():
    sessions = _sqlite_sessions()
    with sessions() as session:
        attempts.upsert_attempt(session, "att-1", 0, status="failed", worker="w1", exception="boom")
        session.commit()
        attempts.upsert_attempt(session, "att-1", 0, status="retried", exception=None, worker=None)
        session.commit()
        rows = attempts.list_attempts(session, "att-1")
        assert [(r.attempt, r.status, r.worker, r.exception) for r in rows] == [(0, "retried", "w1", "boom")]
        session.add(TaskRunAttempt(task_id="att-1", attempt=0, status="dup"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_listener_attempt_rows_for_retry_then_success():
    sessions = _sqlite_sessions()
    config = CeleryManagerConfig(celery_app=None, db_url="sqlite:///:memory:")
    now = datetime.now(timezone.utc).timestamp()
    name = "shop.tasks.sync_orders"
    events = [
        {"type": "task-received", "uuid": "lis-1", "name": name, "retries": 1, "hostname": "w1", "timestamp": now},
        {"type": "task-retried", "uuid": "lis-1", "name": name, "exception": "boom", "traceback": "tb",
         "hostname": "w1", "timestamp": now},
        {"type": "task-succeeded", "uuid": "lis-1", "name": name, "result": "'ok'", "runtime": 1.0,
         "hostname": "w1", "timestamp": now},
    ]
    with sessions() as session:
        for event in events:
            upsert_task_run(session, event, config=config)
            session.commit()
    with sessions() as session:
        assert session.query(TaskRun).filter_by(task_id="lis-1").one().retry_count == 2
        rows = [(r.attempt, r.status) for r in attempts.list_attempts(session, "lis-1")]
    assert rows == [(1, "retried"), (2, "succeeded")]


# --- 9. outbound HTTP calls -------------------------------------------------

def test_http_calls_recorded_only_inside_a_tracked_run():
    import httpx

    from celery_manager.contrib import http

    sessions = _sqlite_sessions()
    app = _eager_app("http-calls")
    configure(sessions, config=CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:"))
    assert http.instrument(requests=False, httpx=True) == ["httpx"]
    try:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        with httpx.Client(transport=transport) as client:
            with progress._run_context("t-1"):
                client.get("https://api.example.com/v1/items?page=2")
            client.get("https://api.example.com/v1/outside")  # no run: nothing stored
        with sessions() as session:
            calls = session.query(ExternalCall).all()
        assert len(calls) == 1
        call = calls[0]
        assert (call.task_id, call.target, call.method, call.status) == (
            "t-1", "https://api.example.com", "GET", 200)
        assert "/v1" not in call.target
    finally:
        http.uninstrument()


# --- 10 to 12. API: snapshots, probes, tokens ----------------------------------

def _app(**overrides):
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:",
                                 secret_key="test-secret", **overrides)
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")  # admin
    return app, session_factory


def _login(app, username="alice"):
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    assert client.post("/api/auth/login", json={"username": username, "password": "pw"}).status_code == 200
    return client


def test_workers_read_serves_snapshot_without_inspecting(monkeypatch):
    from datetime import datetime, timezone

    from celery_manager.api.routes import workers as worker_routes

    app, session_factory = _app(inspect_source="snapshot")
    with session_factory() as session:
        snapshots._store(session, "workers", [{"hostname": "w1@host"}], datetime.now(timezone.utc))

    def no_inspect(*args, **kwargs):
        raise AssertionError("inspect must not run when reads come from a snapshot")

    monkeypatch.setattr(worker_routes, "inspect_workers", no_inspect)
    response = _login(app).get("/api/workers")
    assert response.status_code == 200
    body = response.json()
    assert body["workers"] == [{"hostname": "w1@host"}]
    assert body["snapshot"] is True


def test_livez_and_readyz_need_no_credentials():
    app, _ = _app()
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_api_token_lifecycle_and_restrictions():
    app, _ = _app()
    admin = _login(app)
    created = admin.post("/api/tokens", json={"name": "ci", "role": "operator"})
    assert created.status_code == 200
    token = created.json()["token"]
    assert token.startswith("cmk_")
    bearer = {"Authorization": f"Bearer {token}"}
    script = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)

    assert script.get("/api/tasks", headers=bearer).status_code == 200
    assert script.get("/api/whoami", headers=bearer).json()["username"] == "token:ci"
    assert script.get("/api/tokens", headers=bearer).status_code == 403
    assert script.post("/api/tokens", headers=bearer, json={"name": "x", "role": "viewer"}).status_code == 403

    token_id = created.json()["id"]
    assert admin.delete(f"/api/tokens/{token_id}").status_code == 200
    assert script.get("/api/tasks", headers=bearer).status_code == 401


def test_api_token_refused_under_tenant_scoping():
    config_kwargs = {"tenant_resolver": lambda request, session: "acme"}
    app, session_factory = _app(**config_kwargs)
    with session_factory() as session:
        _, token = api_tokens.create_token(session, "ci", "admin", "alice")
    script = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    response = script.get("/api/tasks", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
