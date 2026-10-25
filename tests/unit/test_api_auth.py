from http.cookies import SimpleCookie

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.sessions import sign_session
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import User


# `base_url` is https, not the TestClient default of http://testserver: the
# session cookie is set with `Secure` by default (CeleryManagerConfig.
# cookie_secure), and an http client correctly refuses to send a Secure
# cookie back — so an http TestClient would 401 on every authenticated
# request. Testing over https keeps the suite exercising the real production
# default instead of switching the flag off to make tests pass.
def _client(**config_kwargs):
    config = CeleryManagerConfig(
        celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret", **config_kwargs
    )
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "correct-horse-battery-staple")
    return TestClient(app, base_url="https://testserver")


def _cm_session(set_cookie: str):
    # Parsed rather than substring-matched: `"Path=/" in header` would also
    # pass for `Path=/anything`.
    return SimpleCookie(set_cookie)["cm_session"]


def test_login_succeeds_with_correct_credentials():
    client = _client()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    assert response.status_code == 200
    assert "cm_session" in response.cookies


def test_login_fails_with_wrong_password():
    client = _client()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert response.status_code == 401


def test_protected_route_requires_session():
    client = _client()
    response = client.get("/api/whoami")
    assert response.status_code == 401


def test_protected_route_succeeds_after_login():
    client = _client()
    client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    response = client.get("/api/whoami")
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}


# --- I7: the session cookie is a bearer credential for the whole dashboard
# and was set without `Secure`, with no way to configure it. ---


def test_session_cookie_is_secure_by_default():
    client = _client()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert "Secure" in set_cookie
    # The flags that were already there must survive.
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie


def test_session_cookie_secure_flag_can_be_disabled_for_local_http_dev():
    client = _client(cookie_secure=False)
    response = client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert "Secure" not in set_cookie
    assert "HttpOnly" in set_cookie


def test_secure_cookie_is_not_sent_back_over_plain_http():
    """Why the default matters, demonstrated rather than asserted: a browser
    (and httpx) will not return a Secure cookie over http, so a plain-HTTP
    deployment that leaves the default on simply won't stay logged in — which
    is the correct, loud failure mode, versus silently leaking the credential."""
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "correct-horse-battery-staple")

    http_client = TestClient(app, base_url="http://testserver")
    assert http_client.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"}
    ).status_code == 200
    assert http_client.get("/api/whoami").status_code == 401


# --- Security pass for 0.1.0a1 ---


@pytest.mark.parametrize("secret_key", ["change-me", "", "   "])
def test_create_app_refuses_a_default_or_empty_secret_key(secret_key):
    """`change-me` is public, so a dashboard started with it accepts a cookie
    anyone can sign for any username. It must not start at all."""
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key=secret_key)
    with pytest.raises(ValueError, match="secret_key"):
        create_app(config)


def test_create_app_refuses_the_dataclass_default_secret_key():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:")
    with pytest.raises(ValueError, match="secret_key"):
        create_app(config)


def test_worker_setup_still_accepts_the_default_secret_key():
    """Workers and the events listener never sign a session, so the check
    lives in create_app(), not the config — they keep working unchanged."""
    import celery_manager

    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:")
    assert celery_manager.setup(config) is not None


def test_login_with_an_over_long_password_is_a_401_not_a_500():
    """bcrypt>=5 raises on >72 bytes. That used to surface as a 500 for an
    existing user and a 401 for a missing one — a username oracle."""
    client = _client()
    for username in ("alice", "nobody"):
        response = client.post("/api/auth/login", json={"username": username, "password": "a" * 100})
        assert response.status_code == 401, username


def test_deleted_user_can_no_longer_use_their_session_cookie():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "correct-horse-battery-staple")
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    assert client.get("/api/whoami").status_code == 200

    with session_factory() as session:
        session.query(User).filter_by(username="alice").delete()
        session.commit()

    assert client.get("/api/whoami").status_code == 401


def test_validly_signed_cookie_for_a_nonexistent_user_is_rejected():
    client = _client()
    client.cookies.set("cm_session", sign_session("test-secret", "ghost"))
    assert client.get("/api/whoami").status_code == 401


def test_session_cookie_is_named_cm_session():
    """Not `session`, which Flask's and Starlette's own session cookies use
    on the same host when celery-manager is mounted inside another app."""
    client = _client()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith("cm_session=")
    assert _cm_session(set_cookie)["path"] == "/"
    # The old name is no longer read.
    fresh = _client()
    fresh.cookies.set("session", response.cookies["cm_session"])
    assert fresh.get("/api/whoami").status_code == 401


def test_session_cookie_path_is_scoped_to_the_mount_point():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "correct-horse-battery-staple")
    client = TestClient(app, base_url="https://testserver", root_path="/celery")

    login = client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    assert login.status_code == 200
    assert _cm_session(login.headers["set-cookie"])["path"] == "/celery"

    # Deletion must use the same path, or the browser keeps the cookie.
    logout = client.post("/api/auth/logout")
    assert _cm_session(logout.headers["set-cookie"])["path"] == "/celery"


def test_logout_deletes_the_cookie_with_the_same_attributes_it_was_set_with():
    client = _client()
    client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    set_cookie = client.post("/api/auth/logout").headers["set-cookie"]
    assert set_cookie.startswith("cm_session=")
    assert "Max-Age=0" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" in set_cookie
    assert _cm_session(set_cookie)["path"] == "/"
