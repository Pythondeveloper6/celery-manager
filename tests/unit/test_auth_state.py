"""Database-backed auth behavior across independent API instances."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from sqlalchemy import select

from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.api.deps import SESSION_COOKIE
from celery_manager.auth.external import ExternalUser
from celery_manager.auth.state import admit_login, cleanup, create_session, now
from celery_manager.auth.users import create_user
from celery_manager.db.auth_state import BrowserSession, LoginBackoff


@pytest.fixture
def environment(tmp_path):
    config = CeleryManagerConfig(celery_app=Celery("auth_state", broker="memory://"),
                                db_url=f"sqlite:///{tmp_path / 'auth.db'}", secret_key="auth-state-test-secret")
    app, sessions = create_app(config)
    with sessions() as session:
        create_user(session, "alice", "pw")
        create_user(session, "bob", "pw")
    return config, app, sessions


def login(app, username="alice"):
    client = TestClient(app, base_url="https://testserver")
    result = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    assert result.status_code == 200, result.text
    return client


def test_unique_sessions_logout_rejects_stolen_cookie_across_api_instances(environment):
    config, app, _ = environment
    a, b = login(app), login(app)
    cookie_a, cookie_b = a.cookies[SESSION_COOKIE], b.cookies[SESSION_COOKIE]
    assert cookie_a != cookie_b
    app2, _ = create_app(config)
    stolen = TestClient(app2, base_url="https://testserver")
    stolen.cookies.set(SESSION_COOKIE, cookie_a)
    assert stolen.get("/api/whoami").status_code == 200
    assert a.post("/api/auth/logout").status_code == 200
    assert stolen.get("/api/whoami").status_code == 401
    assert b.get("/api/whoami").status_code == 200


def test_owned_session_listing_revoke_and_other_account_isolation(environment):
    _, app, sessions = environment
    a, a2, b = login(app), login(app), login(app, "bob")
    rows = a.get("/api/auth/sessions").json()["sessions"]
    assert len(rows) == 2 and sum(row["current"] for row in rows) == 1
    target = next(row for row in rows if not row["current"])
    assert b.post(f"/api/auth/sessions/{target['session_id']}/revoke").status_code == 404
    assert a.post(f"/api/auth/sessions/{target['session_id']}/revoke").status_code == 200
    assert a2.get("/api/whoami").status_code == 401
    assert a.get("/api/whoami").status_code == 200
    with sessions() as db:
        from celery_manager.db.models import AuditLog
        assert db.scalar(select(AuditLog).where(AuditLog.action == "revoke_session")).username == "alice"


def test_expired_and_deleted_session_rows_fail_closed(environment):
    _, app, sessions = environment
    client = login(app)
    with sessions() as db:
        db.scalar(select(BrowserSession)).expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/whoami").status_code == 401
    client = login(app)
    with sessions() as db:
        db.query(BrowserSession).delete()
        db.commit()
    assert client.get("/api/whoami").status_code == 401


def test_login_backoff_shared_by_api_instances_and_reset_after_success(environment):
    config, app, sessions = environment
    app2, _ = create_app(config)
    clients = [TestClient(a, base_url="https://testserver") for a in (app, app2)]
    for i in range(config.login_backoff_threshold):
        result = clients[i % 2].post("/api/auth/login", json={"username": "alice", "password": "bad"})
        assert result.status_code == 401
    blocked = clients[0].post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    # Set times deterministically instead of sleeping through the backoff.
    with sessions() as db:
        for row in db.scalars(select(LoginBackoff)):
            row.blocked_until = now() - timedelta(seconds=1)
        db.commit()
    assert clients[1].post("/api/auth/login", json={"username": "alice", "password": "pw"}).status_code == 200
    with sessions() as db:
        assert sorted(db.scalars(select(LoginBackoff.attempts))) == [0, 5]


def test_login_admission_concurrent_first_sighting_is_atomic(environment):
    config, _, sessions = environment
    def reserve(_):
        with sessions() as db:
            return admit_login(db, config, "target", "peer")[0]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(reserve, range(20)))
    assert sum(wait == 0 for wait in results) == config.login_backoff_threshold
    with sessions() as db:
        assert list(db.scalars(select(LoginBackoff.attempts))) == [config.login_backoff_threshold] * 2


def test_peer_budget_tracks_failures_across_unknown_accounts(environment):
    config, app, _ = environment
    client = TestClient(app, base_url="https://testserver")
    for i in range(config.login_backoff_threshold):
        assert client.post("/api/auth/login", json={"username": f"unknown{i}", "password":"bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"username":"another", "password":"bad"}).status_code == 429


def test_successful_logins_do_not_consume_shared_peer_failure_budget(environment):
    _, app, sessions = environment
    for _ in range(8):
        login(app)
    with sessions() as db:
        assert list(db.scalars(select(LoginBackoff.attempts))) == [0, 0]


def test_bridge_sessions_are_individually_revocable_and_external_auth_is_authoritative(environment):
    config, _, _ = environment
    config.session_bridge_enabled = True
    external = [True]
    config.authenticate_request = lambda _: ExternalUser("host-alice", "admin", subject="host:alice") if external[0] else None
    app, _ = create_app(config)
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/auth/bridge").status_code == 200
    cookie = client.cookies[SESSION_COOKIE]
    external[0] = False
    assert client.get("/api/whoami").status_code == 200
    row = client.get("/api/auth/sessions").json()["sessions"][0]
    assert row["current"]
    assert client.post(f"/api/auth/sessions/{row['session_id']}/revoke").status_code == 200
    assert client.get("/api/whoami").status_code == 401
    # Session revocation does not revoke a still-valid host login.
    external[0] = True
    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get("/api/whoami").status_code == 200


def test_cleanup_prunes_only_expired_sessions_and_stale_backoff(environment):
    config, _, sessions = environment
    with sessions() as db:
        create_session(db, config, "alice", "local")
        expired = BrowserSession(session_id="old", username="alice", source="local", created_at=now()-timedelta(days=20),
                                 expires_at=now()-timedelta(days=1))
        old_attempt = LoginBackoff(key="old", attempts=10, updated_at=now()-timedelta(days=2))
        db.add_all([expired, old_attempt])
        db.commit()
        cleanup(db, config)
        db.commit()
        assert len(list(db.scalars(select(BrowserSession)))) == 1
        assert list(db.scalars(select(LoginBackoff))) == []


def test_v1_aliases_share_session_identity_and_revocation(environment):
    _, app, _ = environment
    client = login(app)
    result = client.get("/api/v1/auth/sessions")
    assert result.status_code == 200
    own = result.json()["sessions"][0]
    assert own["current"]
    assert client.post(f"/api/v1/auth/sessions/{own['session_id']}/revoke").status_code == 200
    assert client.get("/api/v1/whoami").status_code == 401
    assert client.get("/api/whoami").status_code == 401


def test_password_change_keeps_only_fresh_tracked_session(environment):
    _, app, _ = environment
    a, b = login(app), login(app)
    assert a.post("/api/me/password", json={"current_password":"pw", "new_password":"new-password-long"}).status_code == 200
    assert b.get("/api/whoami").status_code == 401
    rows = a.get("/api/auth/sessions").json()["sessions"]
    assert len(rows) == 1 and rows[0]["current"]
    assert a.post("/api/auth/logout").status_code == 200


def test_success_uses_same_sorted_lock_order_and_preserves_account_row(environment, monkeypatch):
    from celery_manager.auth import state
    config, _, sessions = environment
    locked = []
    real = state._locked_backoff
    def observe(session, key):
        locked.append(key)
        return real(session, key)
    monkeypatch.setattr(state, "_locked_backoff", observe)
    with sessions() as db:
        state.login_succeeded(db, ["z-account", "a-peer"], config)
        assert locked == ["a-peer", "z-account"]
        assert db.get(LoginBackoff, "z-account").attempts == 0
