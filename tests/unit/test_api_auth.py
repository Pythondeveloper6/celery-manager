from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig


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


def test_login_succeeds_with_correct_credentials():
    client = _client()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    assert response.status_code == 200
    assert "session" in response.cookies


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
