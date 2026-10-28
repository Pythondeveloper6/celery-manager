"""The dashboard mounted under a prefix inside a host app.

FastAPI: Starlette's own Mount. Flask: a2wsgi + werkzeug's DispatcherMiddleware.
Both are driven end to end (index -> assets -> login -> authenticated call).
"""

from __future__ import annotations

import re
import secrets
from http.cookies import SimpleCookie

import pytest
from celery import Celery
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig

PREFIX = "/celery-manager"
PASSWORD = "mount-test-password"


@pytest.fixture
def dashboard():
    celery_app = Celery("mount-test", broker="memory://")
    config = CeleryManagerConfig(
        celery_app=celery_app,
        db_url="sqlite:///:memory:",
        secret_key=secrets.token_urlsafe(32),
        cookie_secure=False,  # TestClient speaks plain http
    )
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "admin", PASSWORD)
    return app


def _asset_paths(index_html: str) -> list[str]:
    return re.findall(r'(?:src|href)="([^"]+)"', index_html)


def _exercise(client, prefix: str, origin: str) -> None:
    """Shared flow, parametrised only by how the host app is built.

    `origin` is what a browser on the client's own host would send: the
    POSTs carry it so the CSRF check runs through the mount (and, for
    Flask, through a2wsgi's WSGI->ASGI header translation) rather than
    being skipped as a no-Origin non-browser request."""
    index = client.get(f"{prefix}/")
    assert index.status_code == 200
    assert '<div id="root">' in index.text

    # Assets must be relative (no leading slash), or they resolve against the
    # host's root instead of the mount point.
    paths = _asset_paths(index.text)
    assert paths and all(p.startswith("./") for p in paths), paths
    asset = next(p for p in paths if p.endswith(".js"))
    assert client.get(f"{prefix}/{asset[2:]}").status_code == 200

    # No cookie -> 401, through the prefixed path.
    assert client.get(f"{prefix}/api/whoami").status_code == 401

    credentials = {"username": "admin", "password": PASSWORD}
    # A foreign Origin is refused through the mount too.
    foreign = client.post(f"{prefix}/api/auth/login", json=credentials, headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403

    login = client.post(f"{prefix}/api/auth/login", json=credentials, headers={"Origin": origin})
    assert login.status_code == 200
    # The session cookie is scoped to the mount point, not the whole host.
    assert SimpleCookie(login.headers["set-cookie"])["cm_session"]["path"] == prefix

    whoami = client.get(f"{prefix}/api/whoami")
    assert whoami.status_code == 200
    body = whoami.get_json() if hasattr(whoami, "get_json") else whoami.json()  # Flask vs httpx
    assert body == {"username": "admin", "role": "admin", "source": "local"}

    assert client.post(f"{prefix}/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get(f"{prefix}/api/whoami").status_code == 200  # the refused logout changed nothing
    logout = client.post(f"{prefix}/api/auth/logout", headers={"Origin": origin})
    assert logout.status_code == 200
    assert client.get(f"{prefix}/api/whoami").status_code == 401


def test_fastapi_mount(dashboard):
    host = FastAPI()

    @host.get("/hello")
    def hello():
        return {"hello": "host"}

    host.mount(PREFIX, dashboard)

    client = TestClient(host, follow_redirects=False)
    _exercise(client, PREFIX, origin="http://testserver")  # TestClient's default base_url
    # The host's own routes are untouched by the mount.
    assert client.get("/hello").json() == {"hello": "host"}


def test_fastapi_mount_redirects_missing_trailing_slash(dashboard):
    # Relative asset/API URLs need the slash, so the bare prefix must redirect.
    host = FastAPI()
    host.mount(PREFIX, dashboard)
    response = TestClient(host, follow_redirects=False).get(PREFIX)
    assert response.status_code == 307
    assert response.headers["location"].endswith(f"{PREFIX}/")


def test_fastapi_mount_without_cookie_is_rejected_on_other_prefix_path(dashboard):
    # The cookie is Path-scoped to the prefix: a host route must not see it.
    host = FastAPI()
    host.mount(PREFIX, dashboard)
    client = TestClient(host)
    client.post(f"{PREFIX}/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert client.cookies.get("cm_session", path=PREFIX) is not None
    assert client.cookies.get("cm_session", path="/other") is None


def test_flask_mount_via_a2wsgi(dashboard):
    pytest.importorskip("flask")
    a2wsgi = pytest.importorskip("a2wsgi")
    from flask import Flask, jsonify
    from werkzeug.middleware.dispatcher import DispatcherMiddleware

    flask_app = Flask(__name__)

    @flask_app.get("/hello")
    def hello():
        return jsonify(hello="host")

    flask_app.wsgi_app = DispatcherMiddleware(
        flask_app.wsgi_app, {PREFIX: a2wsgi.ASGIMiddleware(dashboard)}
    )

    client = flask_app.test_client()
    _exercise(client, PREFIX, origin="http://localhost")  # werkzeug's test client sends Host: localhost
    assert client.get("/hello").get_json() == {"hello": "host"}


def test_flask_mount_missing_trailing_slash_does_not_serve_broken_page(dashboard):
    pytest.importorskip("flask")
    a2wsgi = pytest.importorskip("a2wsgi")
    from flask import Flask
    from werkzeug.middleware.dispatcher import DispatcherMiddleware

    flask_app = Flask(__name__)
    flask_app.wsgi_app = DispatcherMiddleware(flask_app.wsgi_app, {PREFIX: a2wsgi.ASGIMiddleware(dashboard)})
    response = flask_app.test_client().get(PREFIX)
    assert response.status_code in (301, 302, 307, 308)
    assert response.headers["location"].endswith(f"{PREFIX}/")
