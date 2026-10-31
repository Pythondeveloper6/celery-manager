"""SecurityMiddleware: the Origin check on state-changing requests (CSRF) and
the anti-framing headers on every response."""

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig


@pytest.fixture
def client():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/auth/login", json={"username": "alice", "password": "pw"}).status_code == 200
    return client


def _rebrand(client, **headers):
    return client.put("/api/settings", json={"brand_name": "pwned"}, headers=headers)


# --- CSRF -----------------------------------------------------------------


def test_cross_origin_state_change_is_refused_even_with_a_valid_cookie(client):
    response = _rebrand(client, origin="https://evil.example")
    assert response.status_code == 403
    assert client.get("/api/settings").json()["brand_name"] != "pwned"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_every_unsafe_method_is_checked(client, method):
    response = getattr(client, method)("/api/tasks/abc/cancel", headers={"origin": "https://evil.example"})
    assert response.status_code == 403


def test_cross_origin_login_is_refused(client):
    """Login CSRF: an attacker logging the victim's browser into the
    attacker's account is its own attack, so login is not exempt."""
    client.cookies.clear()
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": "pw"}, headers={"origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert "cm_session" not in response.cookies


def test_null_origin_is_refused(client):
    assert _rebrand(client, origin="null").status_code == 403


def test_same_origin_request_is_allowed(client):
    """What the bundled SPA sends: a same-origin fetch carries Origin."""
    assert _rebrand(client, origin="https://testserver").status_code == 200


def test_default_port_in_host_still_matches_an_origin_without_one(client):
    response = _rebrand(client, origin="https://testserver", host="testserver:443")
    assert response.status_code == 200


def test_different_port_is_a_different_origin(client):
    assert _rebrand(client, origin="https://testserver:8443").status_code == 403


def test_http_default_port_in_host_matches_an_https_origin(client):
    """Upstream TLS terminator -> nginx with `Host $host:$server_port`: the
    app sees the plain-http listener's port 80 for an https page."""
    response = _rebrand(client, origin="https://dash.example.com", host="dash.example.com:80")
    assert response.status_code == 200


def test_https_default_port_in_x_forwarded_host_matches_an_https_origin(client):
    response = _rebrand(
        client, origin="https://dash.example.com", host="127.0.0.1:8000", **{"x-forwarded-host": "dash.example.com:443"}
    )
    assert response.status_code == 200


def test_non_default_origin_port_against_port_less_host_is_refused(client):
    """nginx `$host` drops the port; the fix is `$http_host` on the proxy,
    not treating :8443 and the default port as the same origin."""
    response = _rebrand(client, origin="https://dash.example.com:8443", host="dash.example.com")
    assert response.status_code == 403


def test_non_default_host_port_against_port_less_origin_is_refused(client):
    response = _rebrand(client, origin="https://dash.example.com", host="dash.example.com:8443")
    assert response.status_code == 403


def test_origin_side_default_port_is_scheme_specific(client):
    """`http://x:443` is port 443, not the http default, so it is not `x`."""
    assert _rebrand(client, origin="http://dash.example.com:443", host="dash.example.com").status_code == 403
    assert _rebrand(client, origin="http://dash.example.com:443", host="dash.example.com:443").status_code == 200


@pytest.mark.parametrize(
    ("origin", "host", "status"),
    [
        ("https://[::1]", "[::1]", 200),
        ("https://[::1]", "[::1]:443", 200),
        ("https://[::1]", "[::1]:80", 200),
        ("https://[::1]:8443", "[::1]:8443", 200),
        ("https://[::1]:8443", "[::1]", 403),
        ("https://[::1]", "[::2]", 403),
        # The address's own trailing group must not be read as a port.
        ("https://[::80]", "[::80]", 200),
        ("https://[::]", "[::80]", 403),
    ],
)
def test_ipv6_literal_hosts(client, origin, host, status):
    assert _rebrand(client, origin=origin, host=host).status_code == status


def test_request_without_origin_is_allowed(client):
    """curl, scripts and other non-browser clients send no Origin and can't
    be made to carry a victim's cookie, so they are not affected."""
    assert _rebrand(client).status_code == 200


def test_proxy_rewritten_host_matches_via_x_forwarded_host(client):
    response = _rebrand(
        client, origin="https://dash.example.com", host="127.0.0.1:8000", **{"x-forwarded-host": "dash.example.com"}
    )
    assert response.status_code == 200


def test_cross_origin_get_is_not_blocked(client):
    """Reads are side-effect free; refusing them would only break links."""
    response = client.get("/api/whoami", headers={"origin": "https://evil.example"})
    assert response.status_code == 200


# --- Anti-framing headers -------------------------------------------------


@pytest.mark.parametrize("path", ["/api/settings", "/api/whoami", "/"])
def test_responses_forbid_framing(client, path):
    response = client.get(path)
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_refused_csrf_response_also_forbids_framing(client):
    response = _rebrand(client, origin="https://evil.example")
    assert response.headers["x-frame-options"] == "DENY"
