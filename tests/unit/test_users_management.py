"""Dashboard user management: CRUD with audit rows, the last-admin rules,
session invalidation on password change, and role enforcement."""

from __future__ import annotations

import json

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.sessions import sign_session
from celery_manager.auth.users import MIN_PASSWORD_LENGTH, create_user, verify_password
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.users import UserConflict, delete_user
from celery_manager.db.models import AuditLog, User

PW = "correct-horse-battery"


@pytest.fixture
def env():
    config = CeleryManagerConfig(
        celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret"
    )
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "root", PW, role="admin")
        create_user(session, "boss", PW, role="admin")
        create_user(session, "op", PW, role="operator")
        create_user(session, "view", PW, role="viewer")
    return app, session_factory


def client_for(app, username, password=PW):
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    return client


def audit_rows(session_factory, action):
    with session_factory() as session:
        rows = session.query(AuditLog).filter_by(action=action).order_by(AuditLog.id).all()
        return [(r.username, r.target_type, r.target_id, json.loads(r.details) if r.details else None) for r in rows]


# --- list / create -----------------------------------------------------------


def test_list_users_has_source_local(env):
    app, _ = env
    body = client_for(app, "root").get("/api/users").json()
    assert body["users"] == [
        {"username": "boss", "role": "admin", "source": "local"},
        {"username": "op", "role": "operator", "source": "local"},
        {"username": "root", "role": "admin", "source": "local"},
        {"username": "view", "role": "viewer", "source": "local"},
    ]


def test_create_user_can_sign_in_and_is_audited(env):
    app, sf = env
    response = client_for(app, "root").post(
        "/api/users", json={"username": "new.user@example.com", "password": "a-fine-password", "role": "operator"}
    )
    assert response.status_code == 201
    assert response.json() == {"username": "new.user@example.com", "role": "operator", "source": "local"}
    assert client_for(app, "new.user@example.com", "a-fine-password").get("/api/whoami").json()["role"] == "operator"
    assert audit_rows(sf, "create_user") == [("root", "user", "new.user@example.com", {"role": "operator"})]


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"username": "x y", "password": "a-fine-password"}, "username"),
        ({"username": "", "password": "a-fine-password"}, "username"),
        ({"username": "a/b", "password": "a-fine-password"}, "username"),
        ({"username": "ok", "password": "short"}, f"at least {MIN_PASSWORD_LENGTH}"),
        ({"username": "ok", "password": "a" * 73}, "at most 72"),
        ({"username": "ok", "password": "a-fine-password", "role": "root"}, "unknown role"),
    ],
)
def test_create_user_validation(env, body, fragment):
    app, sf = env
    response = client_for(app, "root").post("/api/users", json=body)
    assert response.status_code == 422
    assert fragment in response.json()["detail"]
    assert audit_rows(sf, "create_user") == []


def test_create_duplicate_is_a_conflict(env):
    app, _ = env
    response = client_for(app, "root").post("/api/users", json={"username": "op", "password": "a-fine-password"})
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


# --- roles -------------------------------------------------------------------


def test_change_role_applies_and_is_audited(env):
    app, sf = env
    root = client_for(app, "root")
    view = client_for(app, "view")
    assert root.patch("/api/users/view", json={"role": "operator"}).json()["role"] == "operator"
    assert view.get("/api/whoami").json()["role"] == "operator"
    assert audit_rows(sf, "set_role") == [("root", "user", "view", {"from": "viewer", "to": "operator"})]
    # Setting the role it already has is a no-op: nothing more to audit.
    root.patch("/api/users/view", json={"role": "operator"})
    assert len(audit_rows(sf, "set_role")) == 1


def test_change_role_errors(env):
    app, _ = env
    root = client_for(app, "root")
    assert root.patch("/api/users/ghost", json={"role": "viewer"}).status_code == 404
    assert root.patch("/api/users/view", json={"role": "root"}).status_code == 422


def test_last_admin_cannot_be_demoted_but_can_once_another_exists(env):
    app, sf = env
    root = client_for(app, "root")
    assert root.patch("/api/users/boss", json={"role": "viewer"}).status_code == 200
    # root is now the only admin; demoting itself is refused.
    response = root.patch("/api/users/root", json={"role": "viewer"})
    assert response.status_code == 409
    assert "only admin" in response.json()["detail"]
    with sf() as session:
        assert session.query(User).filter_by(username="root").one().role == "admin"
    # Promote another, then self-demotion is fine.
    assert root.patch("/api/users/op", json={"role": "admin"}).status_code == 200
    assert root.patch("/api/users/root", json={"role": "viewer"}).status_code == 200


# --- delete ------------------------------------------------------------------


def test_delete_user_signs_them_out_and_is_audited(env):
    app, sf = env
    root = client_for(app, "root")
    view = client_for(app, "view")
    assert root.delete("/api/users/view").status_code == 200
    assert view.get("/api/whoami").status_code == 401
    assert audit_rows(sf, "delete_user") == [("root", "user", "view", {"role": "viewer"})]
    assert root.delete("/api/users/view").status_code == 404


def test_cannot_delete_yourself(env):
    app, sf = env
    response = client_for(app, "root").delete("/api/users/root")
    assert response.status_code == 409
    assert "own account" in response.json()["detail"]
    assert audit_rows(sf, "delete_user") == []


def test_one_admin_can_delete_another_but_never_the_last(env):
    app, sf = env
    assert client_for(app, "root").delete("/api/users/boss").status_code == 200
    # No other admin is left to even attempt it over the API, so go through
    # the control layer to prove the rule holds without the self-delete check.
    with sf() as session, pytest.raises(UserConflict, match="only admin"):
        delete_user(session, "someone-else", "root")


# --- password reset ----------------------------------------------------------


def test_reset_password_invalidates_old_session_and_new_password_works(env):
    app, sf = env
    root = client_for(app, "root")
    old = client_for(app, "op")
    old_cookie = old.cookies.get("cm_session")
    response = root.post("/api/users/op/password", json={"password": "brand-new-password"})
    assert response.status_code == 200
    assert response.json() == {"username": "op", "generated_password": None}
    assert response.headers["cache-control"] == "no-store"
    # The cookie issued before the reset is dead, whoever holds it.
    assert old.get("/api/whoami").status_code == 401
    stolen = TestClient(app, base_url="https://testserver")
    stolen.cookies.set("cm_session", old_cookie)
    assert stolen.get("/api/whoami").status_code == 401
    assert client_for(app, "op", "brand-new-password").get("/api/whoami").status_code == 200
    with sf() as session:
        assert verify_password(session, "op", PW) is None
    assert audit_rows(sf, "reset_password") == [("root", "user", "op", {"generated": False})]


def test_generated_password_is_returned_once_and_never_audited(env):
    app, sf = env
    response = client_for(app, "root").post("/api/users/view/password", json={"generate": True})
    generated = response.json()["generated_password"]
    assert len(generated) >= MIN_PASSWORD_LENGTH
    assert client_for(app, "view", generated).get("/api/whoami").status_code == 200
    with sf() as session:
        details = [r.details or "" for r in session.query(AuditLog).all()]
    assert not any(generated in d for d in details)


def test_supplied_password_is_never_audited(env):
    app, sf = env
    client_for(app, "root").post("/api/users/view/password", json={"password": "very-secret-value"})
    with sf() as session:
        rows = session.query(AuditLog).all()
    assert not any("very-secret-value" in (r.details or "") for r in rows)


@pytest.mark.parametrize(
    "body",
    [{}, {"password": "a-fine-password", "generate": True}, {"password": "short"}],
)
def test_reset_password_validation(env, body):
    app, sf = env
    assert client_for(app, "root").post("/api/users/op/password", json=body).status_code == 422
    assert audit_rows(sf, "reset_password") == []


def test_reset_unknown_user_and_self(env):
    app, _ = env
    root = client_for(app, "root")
    assert root.post("/api/users/ghost/password", json={"generate": True}).status_code == 404
    response = root.post("/api/users/root/password", json={"generate": True})
    assert response.status_code == 409
    assert "Change password" in response.json()["detail"]


# --- own password ------------------------------------------------------------


def test_change_own_password_needs_current_and_keeps_this_session(env):
    app, sf = env
    me = client_for(app, "view")
    other = client_for(app, "view")
    bad = me.post("/api/me/password", json={"current_password": "wrong", "new_password": "another-password"})
    assert bad.status_code == 403
    assert "current password" in bad.json()["detail"]
    assert audit_rows(sf, "change_password") == []

    ok = me.post("/api/me/password", json={"current_password": PW, "new_password": "another-password"})
    assert ok.status_code == 200
    # This browser keeps working through the re-issued cookie; others don't.
    assert me.get("/api/whoami").status_code == 200
    assert other.get("/api/whoami").status_code == 401
    assert client_for(app, "view", "another-password").get("/api/whoami").status_code == 200
    assert audit_rows(sf, "change_password") == [("view", "user", "view", None)]


def test_change_own_password_validates_the_new_one(env):
    app, _ = env
    response = client_for(app, "view").post(
        "/api/me/password", json={"current_password": PW, "new_password": "short"}
    )
    assert response.status_code == 422


# --- roles & sessions --------------------------------------------------------


@pytest.mark.parametrize("who", ["op", "view"])
def test_non_admins_are_refused(env, who):
    app, _ = env
    client = client_for(app, who)
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/users", json={"username": "z", "password": "a-fine-password"}).status_code == 403
    assert client.patch("/api/users/view", json={"role": "admin"}).status_code == 403
    assert client.post("/api/users/view/password", json={"generate": True}).status_code == 403
    assert client.delete("/api/users/view").status_code == 403


def test_unauthenticated_is_refused(env):
    app, _ = env
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/users").status_code == 401
    assert client.post("/api/me/password", json={"current_password": "a", "new_password": "b"}).status_code == 401


def test_a_session_without_a_fingerprint_is_rejected(env):
    # What a cookie issued by an older version looks like.
    app, _ = env
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("cm_session", sign_session("test-secret", "root"))
    assert client.get("/api/whoami").status_code == 401
