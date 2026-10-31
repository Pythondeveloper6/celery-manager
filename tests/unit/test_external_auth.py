"""Signing in with the host app's users: `CeleryManagerConfig.authenticate_request`.

The authenticator here stands in for a host app: it reads the host's own
`host_session` cookie and looks the user up in a dict the tests mutate, the
way a host's user table would change between requests.
"""

import logging

import pytest
from celery import Celery
from fastapi import HTTPException
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import AuditLog
from celery_manager.integrations import ExternalUser

HOST_COOKIE = "host_session"


class Host:
    """A fake host app: `users` maps the host session cookie's value to
    (username, role). Counts calls so tests can tell whether it was asked."""

    def __init__(self):
        self.users = {}
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        token = request.cookies.get(HOST_COOKIE)
        if token is None or token not in self.users:
            return None
        username, role = self.users[token]
        return ExternalUser(username=username, role=role)


def _build(authenticate=None, *, local_password="pw", **config_kwargs):
    config = CeleryManagerConfig(
        celery_app=Celery("test"),
        db_url="sqlite:///:memory:",
        secret_key="test-secret",
        authenticate_request=authenticate,
        **config_kwargs,
    )
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "local-admin", local_password, role="admin")
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    return client, session_factory


def _sign_in_to_host(client, token):
    client.cookies.set(HOST_COOKIE, token)


def _sign_in_locally(client):
    assert client.post("/api/auth/login", json={"username": "local-admin", "password": "pw"}).status_code == 200


# --- who the request belongs to ----------------------------------------------


def test_host_user_is_signed_in_with_the_role_the_host_gives():
    host = Host()
    host.users["t1"] = ("dana", "operator")
    client, _ = _build(host)
    _sign_in_to_host(client, "t1")

    response = client.get("/api/whoami")

    assert response.status_code == 200
    assert response.json() == {"username": "dana", "role": "operator", "source": "external"}


def test_host_answer_wins_over_a_local_session_in_the_same_browser():
    host = Host()
    host.users["t1"] = ("dana", "viewer")
    client, _ = _build(host)
    _sign_in_locally(client)
    _sign_in_to_host(client, "t1")

    assert client.get("/api/whoami").json() == {"username": "dana", "role": "viewer", "source": "external"}


def test_not_signed_in_to_the_host_falls_back_to_local_accounts():
    host = Host()
    client, _ = _build(host)

    assert client.get("/api/whoami").status_code == 401
    _sign_in_locally(client)
    assert client.get("/api/whoami").json() == {"username": "local-admin", "role": "admin", "source": "local"}
    assert host.calls >= 2  # asked every time, even when it says no


def test_host_user_with_no_role_gets_403_and_no_fallback_to_a_local_session():
    host = Host()
    host.users["t1"] = ("eve", None)
    client, _ = _build(host, login_provider_name="Acme")
    # A local admin session in the same browser must not be a way around
    # the host saying "no".
    _sign_in_locally(client)
    _sign_in_to_host(client, "t1")

    whoami = client.get("/api/whoami")
    assert whoami.status_code == 403
    assert whoami.json()["detail"] == "Your Acme account (eve) has no access to this dashboard."
    assert client.get("/api/tasks").status_code == 403
    assert client.put("/api/settings", json={"brand_name": "x"}).status_code == 403


def test_role_changes_on_the_host_apply_on_the_next_request():
    host = Host()
    host.users["t1"] = ("dana", "admin")
    client, _ = _build(host)
    _sign_in_to_host(client, "t1")
    assert client.put("/api/settings", json={"brand_name": "Ops"}).status_code == 200

    host.users["t1"] = ("dana", "viewer")
    refused = client.put("/api/settings", json={"brand_name": "Ops 2"})
    assert refused.status_code == 403
    assert "needs the admin role (you are viewer)" in refused.json()["detail"]

    del host.users["t1"]
    assert client.get("/api/whoami").status_code == 401


def test_the_host_is_asked_once_per_request():
    """`Principal` is used by both `require_role` and the route; FastAPI's
    per-request dependency cache must keep that to one host lookup."""
    host = Host()
    host.users["t1"] = ("dana", "admin")
    client, _ = _build(host)
    _sign_in_to_host(client, "t1")
    host.calls = 0

    assert client.post("/api/me/password", json={"current_password": "a", "new_password": "b" * 8}).status_code == 409
    assert host.calls == 1


def test_async_authenticator_is_awaited():
    async def authenticate(request):
        return ExternalUser("async-user", "viewer") if request.cookies.get(HOST_COOKIE) else None

    client, _ = _build(authenticate)
    _sign_in_to_host(client, "anything")
    assert client.get("/api/whoami").json()["username"] == "async-user"


# --- fail closed --------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        ExternalUser("dana", "superuser"),
        ExternalUser("dana", "Admin"),
        ExternalUser("", "admin"),
        ExternalUser("dana\nadmin", "admin"),
        ExternalUser("x" * 256, "admin"),
        {"username": "dana", "role": "admin"},
        "dana",
    ],
    ids=["unknown-role", "wrong-case", "empty-name", "control-char", "too-long", "dict", "str"],
)
def test_an_unusable_answer_is_a_server_error_not_access(answer, caplog):
    client, _ = _build(lambda request: answer)
    # Even with a valid local session: a broken host mapping must surface,
    # not quietly fall back to some other identity.
    _sign_in_locally(client)

    with caplog.at_level(logging.ERROR, logger="celery_manager.auth.external"):
        response = client.get("/api/whoami")

    assert response.status_code == 500
    assert "misconfigured" in response.json()["detail"]
    assert "authenticate_request returned" in caplog.text


def test_an_authenticator_that_raises_is_a_server_error(caplog):
    def authenticate(request):
        raise RuntimeError("database is down")

    client, _ = _build(authenticate)
    _sign_in_locally(client)

    with caplog.at_level(logging.ERROR, logger="celery_manager.auth.external"):
        response = client.get("/api/whoami")

    assert response.status_code == 500
    assert "database is down" not in response.text  # details stay in the log
    assert "database is down" in caplog.text


def test_an_http_exception_from_the_host_passes_through():
    """A host dependency reused as the authenticator may raise its own 401."""

    def authenticate(request):
        raise HTTPException(status_code=401, detail="token expired")

    client, _ = _build(authenticate)
    response = client.get("/api/whoami")
    assert response.status_code == 401
    assert response.json()["detail"] == "token expired"


# --- local login turned off ----------------------------------------------------


def test_local_login_disabled_refuses_the_password_form_and_old_sessions():
    host = Host()
    client, _ = _build(host, login_provider_name="Acme")
    _sign_in_locally(client)  # a session issued while local login was on
    old_cookie = client.cookies.get("cm_session")

    client2, _ = _build(host, local_login_enabled=False, login_provider_name="Acme")
    login = client2.post("/api/auth/login", json={"username": "local-admin", "password": "pw"})
    assert login.status_code == 403
    assert "Sign in with Acme" in login.json()["detail"]
    assert "cm_session" not in login.cookies

    client2.cookies.set("cm_session", old_cookie)
    assert client2.get("/api/whoami").status_code == 401


def test_local_login_disabled_still_admits_host_users():
    host = Host()
    host.users["t1"] = ("dana", "operator")
    client, _ = _build(host, local_login_enabled=False)
    _sign_in_to_host(client, "t1")
    assert client.get("/api/whoami").json()["source"] == "external"


def test_local_login_cannot_be_disabled_without_an_authenticator():
    config = CeleryManagerConfig(
        celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret", local_login_enabled=False
    )
    with pytest.raises(ValueError, match="nobody could sign in"):
        create_app(config)


def test_authenticate_request_must_be_callable():
    config = CeleryManagerConfig(
        celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret", authenticate_request="no"
    )
    with pytest.raises(ValueError, match="must be a callable"):
        create_app(config)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "//evil.example/login",
        "/\\evil.example/login",
        "accounts/login",
        "https://",
        "/login\n",
        "/log in",
        "ftp://example.com/",
    ],
)
@pytest.mark.parametrize("field", ["login_url", "logout_url"])
def test_unsafe_login_and_logout_urls_are_refused_at_startup(field, url):
    config = CeleryManagerConfig(
        celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret", **{field: url}
    )
    with pytest.raises(ValueError, match=field):
        create_app(config)


@pytest.mark.parametrize("url", ["/accounts/login/?next={next}", "https://sso.example.com/login?return={next}"])
def test_paths_and_http_urls_are_accepted(url):
    _build(Host(), login_url=url, logout_url=url)


# --- what the dashboard is told -----------------------------------------------


def test_auth_options_without_an_authenticator():
    client, _ = _build()
    assert client.get("/api/auth/options").json() == {
        "local_login": True,
        "live_stream": "sse", "allow_queue_override": False, "retry_edit_args": True,
        "environment": "", "monitoring_only": False, "read_only": False,
        "sensitive_actions": [], "tenant_scoped": False,
        "external": False,
        "provider_name": "your app",
        "login_url": None,
        "logout_url": None,
    }


def test_auth_options_are_public_and_describe_the_host():
    client, _ = _build(
        Host(),
        local_login_enabled=False,
        login_url="/accounts/login/?next={next}",
        logout_url="/accounts/logout/",
        login_provider_name="Acme",
    )
    response = client.get("/api/auth/options")  # no session at all
    assert response.status_code == 200
    assert response.json() == {
        "local_login": False,
        "live_stream": "sse", "allow_queue_override": False, "retry_edit_args": True,
        "environment": "", "monitoring_only": False, "read_only": False,
        "sensitive_actions": [], "tenant_scoped": False,
        "external": True,
        "provider_name": "Acme",
        "login_url": "/accounts/login/?next={next}",
        "logout_url": "/accounts/logout/",
    }


def test_external_users_cannot_change_a_password_here():
    """Even one that happens to match a local account of the same name."""
    host = Host()
    host.users["t1"] = ("local-admin", "admin")
    client, _ = _build(host, login_provider_name="Acme")
    _sign_in_to_host(client, "t1")

    response = client.post("/api/me/password", json={"current_password": "pw", "new_password": "new-password-1"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Your password is managed by Acme; change it there."
    # The local account's password is untouched.
    client.cookies.clear()
    _sign_in_locally(client)


def test_external_admin_manages_local_accounts():
    host = Host()
    host.users["t1"] = ("dana", "admin")
    client, _ = _build(host)
    _sign_in_to_host(client, "t1")

    created = client.post("/api/users", json={"username": "bob", "password": "long-enough", "role": "viewer"})
    assert created.status_code == 201
    assert created.json() == {"username": "bob", "role": "viewer", "source": "local"}
    assert {u["username"] for u in client.get("/api/users").json()["users"]} == {"local-admin", "bob"}


# --- audit and CSRF -------------------------------------------------------------


def test_actions_are_audited_as_the_host_username():
    host = Host()
    host.users["t1"] = ("dana@example.com", "admin")
    client, session_factory = _build(host)
    _sign_in_to_host(client, "t1")

    assert client.put("/api/settings", json={"brand_name": "Ops"}).status_code == 200

    with session_factory() as session:
        row = session.query(AuditLog).order_by(AuditLog.id.desc()).first()
    assert (row.username, row.action) == ("dana@example.com", "update")


def test_cross_origin_write_with_the_host_cookie_is_refused_before_the_host_is_asked():
    """The CSRF check is the Origin check in SecurityMiddleware, which runs
    before any authentication and doesn't care which cookie would have
    authenticated the request. A host cookie that lacks SameSite (Flask's
    default) is therefore no weaker here than cm_session."""
    host = Host()
    host.users["t1"] = ("dana", "admin")
    client, _ = _build(host)
    _sign_in_to_host(client, "t1")
    host.calls = 0

    for origin in ("https://evil.example", "https://sub.testserver", "null"):
        response = client.put("/api/settings", json={"brand_name": "pwned"}, headers={"origin": origin})
        assert response.status_code == 403
        assert response.json() == {"detail": "cross-origin request refused"}
    assert host.calls == 0
    assert client.get("/api/settings").json()["brand_name"] != "pwned"


def test_same_origin_write_with_the_host_cookie_is_allowed():
    host = Host()
    host.users["t1"] = ("dana", "admin")
    client, _ = _build(host)
    _sign_in_to_host(client, "t1")
    response = client.put("/api/settings", json={"brand_name": "Ops"}, headers={"origin": "https://testserver"})
    assert response.status_code == 200
