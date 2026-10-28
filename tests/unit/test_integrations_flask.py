"""`celery_manager.integrations.flask`: signing in with a Flask session.

Against a real Flask app, with the dashboard mounted in its process through
a2wsgi + DispatcherMiddleware exactly as docs/integrations.md shows, so the
browser-side cookie handling is Flask's own.
"""

import logging

import pytest

flask = pytest.importorskip("flask")
pytest.importorskip("a2wsgi")

from a2wsgi import ASGIMiddleware  # noqa: E402
from celery import Celery  # noqa: E402
from werkzeug.middleware.dispatcher import DispatcherMiddleware  # noqa: E402

from celery_manager.api.app import create_app  # noqa: E402
from celery_manager.config import CeleryManagerConfig  # noqa: E402
from celery_manager.integrations.flask import flask_authenticator  # noqa: E402


class FakeUser:
    def __init__(self, user_id, username, role, active=True):
        self.id = user_id
        self.username = username
        self.role = role
        self.is_active = active

    def get_id(self):
        return str(self.id)


def _site(*, users, samesite="Lax", **auth_kwargs):
    """A Flask app with a Flask-Login-style login (`session["_user_id"]`)
    and the dashboard mounted at /cm."""
    app = flask.Flask("host")
    app.secret_key = "flask-secret"
    app.config["SESSION_COOKIE_SAMESITE"] = samesite
    loaded_in_app_context = []

    @app.post("/login/<user_id>")
    def login(user_id):
        flask.session["_user_id"] = user_id
        return "ok"

    @app.post("/logout")
    def logout():
        flask.session.clear()
        return "ok"

    def load_user(user_id):
        # What a Flask-Login user_loader does with Flask-SQLAlchemy: needs
        # the app context.
        loaded_in_app_context.append(flask.current_app.name)
        return users.get(user_id)

    authenticate = flask_authenticator(
        app, load_user=load_user, role_for=lambda user: user.role, **auth_kwargs
    )
    dashboard, _ = create_app(
        CeleryManagerConfig(
            celery_app=Celery("test"),
            db_url="sqlite:///:memory:",
            secret_key="test-secret",
            authenticate_request=authenticate,
        )
    )
    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/cm": ASGIMiddleware(dashboard)})
    return app, loaded_in_app_context


def test_flask_user_signed_in_through_the_mounted_dashboard():
    users = {"1": FakeUser(1, "fiona", "operator")}
    app, loaded = _site(users=users)
    client = app.test_client()

    assert client.get("/cm/api/whoami").status_code == 401
    client.post("/login/1")
    response = client.get("/cm/api/whoami")

    assert response.status_code == 200
    assert response.get_json() == {"username": "fiona", "role": "operator", "source": "external"}
    assert loaded == ["host"]


def test_flask_user_without_a_role_gets_403():
    app, _ = _site(users={"2": FakeUser(2, "guest", None)})
    client = app.test_client()
    client.post("/login/2")
    response = client.get("/cm/api/whoami")
    assert response.status_code == 403
    assert "(guest) has no access" in response.get_json()["detail"]


def test_unknown_or_inactive_flask_user_and_signed_out_session():
    users = {"3": FakeUser(3, "gone", "admin", active=False)}
    app, _ = _site(users=users)
    client = app.test_client()

    client.post("/login/3")
    assert client.get("/cm/api/whoami").status_code == 401  # inactive
    client.post("/login/99")
    assert client.get("/cm/api/whoami").status_code == 401  # no such user
    client.post("/logout")
    assert client.get("/cm/api/whoami").status_code == 401


def test_a_forged_flask_cookie_is_not_signed_in():
    """The cookie is only trusted when Flask's own serializer verifies it."""
    other = flask.Flask("other")
    other.secret_key = "attacker-secret"
    with other.test_request_context():
        forged = other.session_interface.get_signing_serializer(other).dumps({"_user_id": "1"})
    app, loaded = _site(users={"1": FakeUser(1, "fiona", "admin")})
    client = app.test_client()
    client.set_cookie("session", forged)
    assert client.get("/cm/api/whoami").status_code == 401
    assert loaded == []


def test_csrf_check_applies_to_writes_authenticated_by_the_flask_session():
    app, _ = _site(users={"1": FakeUser(1, "fiona", "admin")})
    client = app.test_client()
    client.post("/login/1")

    refused = client.put("/cm/api/settings", json={"brand_name": "pwned"}, headers={"Origin": "https://evil.example"})
    assert refused.status_code == 403
    assert refused.get_json() == {"detail": "cross-origin request refused"}

    allowed = client.put("/cm/api/settings", json={"brand_name": "Ops"}, headers={"Origin": "http://localhost"})
    assert allowed.status_code == 200
    assert client.get("/cm/api/settings").get_json()["brand_name"] == "Ops"


def test_username_for_and_user_id_key():
    users = {"7": FakeUser(7, "ignored", "viewer")}
    app = flask.Flask("host")
    app.secret_key = "s"
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    @app.post("/login")
    def login():
        flask.session["uid"] = 7
        return "ok"

    authenticate = flask_authenticator(
        app,
        load_user=users.get,
        role_for=lambda user: user.role,
        username_for=lambda user: f"user-{user.id}",
        user_id_key="uid",
    )
    dashboard, _ = create_app(
        CeleryManagerConfig(
            celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="t", authenticate_request=authenticate
        )
    )
    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/cm": ASGIMiddleware(dashboard)})
    client = app.test_client()
    client.post("/login")
    assert client.get("/cm/api/whoami").get_json()["username"] == "user-7"


def test_warns_when_the_flask_session_cookie_has_no_samesite(caplog):
    with caplog.at_level(logging.WARNING, logger="celery_manager.integrations.flask"):
        _site(users={}, samesite=None)
    assert "SESSION_COOKIE_SAMESITE" in caplog.text
