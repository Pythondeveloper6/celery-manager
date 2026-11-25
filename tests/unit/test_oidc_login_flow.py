"""Authorization code + PKCE sign-in, session role refresh and revocation; no real identity provider."""
import base64
import hashlib
import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from celery import Celery
from fastapi.testclient import TestClient

jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")

from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.providers import OIDCBearerProvider

ISSUER = "https://identity.example"


@pytest.fixture
def env():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k", use="sig", alg="RS256")
    state = {"roles": ["Ops.Read"], "refresh_ok": True, "posts": []}

    def access():
        return jwt.encode({"iss": ISSUER, "aud": "manager", "sub": "person-1", "exp": int(time.time()) + 60,
                           "preferred_username": "dana", "roles": state["roles"]}, key, algorithm="RS256",
                          headers={"kid": "k"})

    def fetch_json(url):
        assert url.endswith("/.well-known/openid-configuration")
        return {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/keys"}

    def post_form(url, data, auth=None):
        state["posts"].append((url, data, auth))
        if data["grant_type"] == "refresh_token" and not state["refresh_ok"]:
            raise OSError("invalid_grant")
        return {"access_token": access(), "refresh_token": "r-" + str(len(state["posts"]))}

    provider = OIDCBearerProvider(issuer=ISSUER, audience="manager", role_map={"Ops.Admin": "admin", "Ops.Read": "viewer"},
                                  leeway_s=0, fetch_json=fetch_json, post_form=post_form)
    config = CeleryManagerConfig(celery_app=Celery("flow"), db_url="sqlite:///:memory:", secret_key="s",
                                 local_login_enabled=False, principal_providers=(provider,),
                                 oidc_client_id="dash", oidc_client_secret="shh", oidc_role_refresh_seconds=900,
                                 dashboard_url="https://dash.example")
    app, _ = create_app(config)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    with patch.object(jwt.PyJWKClient, "fetch_data", return_value={"keys": [jwk]}):
        yield client, state, config, app


def sign_in(client, state):
    login = client.get("/api/auth/oidc/login")
    assert login.status_code == 303
    query = parse_qs(urlparse(login.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://dash.example/api/auth/oidc/callback"]
    callback = client.get("/api/auth/oidc/callback", params={"code": "c1", "state": query["state"][0]})
    return query, callback


def test_login_exchanges_code_with_pkce_and_signs_in(env):
    client, state, *_ = env
    assert client.get("/api/whoami").status_code == 401
    query, callback = sign_in(client, state)
    assert callback.status_code == 303
    url, data, auth = state["posts"][0]
    assert url == ISSUER + "/token" and auth == ("dash", "shh") and data["code"] == "c1"
    digest = hashlib.sha256(data["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"][0]
    me = client.get("/api/whoami").json()
    assert (me["username"], me["role"], me["source"]) == ("dana", "viewer", "external")
    assert client.get("/api/auth/options").json()["oidc_login_url"]


def test_callback_rejects_wrong_state_and_missing_role(env):
    client, state, *_ = env
    client.get("/api/auth/oidc/login")
    assert client.get("/api/auth/oidc/callback", params={"code": "c", "state": "forged"}).status_code == 400
    assert not state["posts"]
    state["roles"] = []
    _, callback = sign_in(client, state)
    assert callback.status_code == 403
    assert client.get("/api/whoami").status_code == 401


def test_role_is_refreshed_when_due_and_removal_ends_session(env):
    client, state, config, _ = env
    sign_in(client, state)
    state["roles"] = ["Ops.Read", "Ops.Admin"]
    config.oidc_role_refresh_seconds = 1
    assert client.get("/api/whoami").json()["role"] == "viewer"  # not yet due
    time.sleep(1.1)
    assert client.get("/api/whoami").json()["role"] == "admin"
    assert state["posts"][-1][1]["grant_type"] == "refresh_token" and state["posts"][-1][1]["refresh_token"] == "r-1"
    state["roles"] = []
    time.sleep(1.1)
    assert client.get("/api/whoami").status_code == 401
    state["roles"] = ["Ops.Read"]
    assert client.get("/api/whoami").status_code == 401  # revoked for good


def test_failed_refresh_ends_session_and_logout_revokes(env):
    client, state, config, _ = env
    sign_in(client, state)
    config.oidc_role_refresh_seconds = 1
    state["refresh_ok"] = False
    time.sleep(1.1)
    assert client.get("/api/whoami").status_code == 401
    state["refresh_ok"] = True
    config.oidc_role_refresh_seconds = 900
    sign_in(client, state)
    cookie = client.cookies.get("cm_session")
    assert client.get("/api/whoami").status_code == 200
    client.post("/api/auth/logout")
    client.cookies.set("cm_session", cookie)
    assert client.get("/api/whoami").status_code == 401


def test_bridge_reports_renewal_point_and_options_expose_mode():
    config = CeleryManagerConfig(celery_app=Celery("m"), db_url="sqlite:///:memory:", secret_key="s", auth_mode="bearer")
    app, _ = create_app(config)
    assert TestClient(app).get("/api/auth/options").json()["auth_mode"] == "bearer"
    with pytest.raises(ValueError):
        CeleryManagerConfig(celery_app=Celery("m"), auth_mode="jar")
    with pytest.raises(ValueError):
        CeleryManagerConfig(celery_app=Celery("m"), oidc_client_id="x")
