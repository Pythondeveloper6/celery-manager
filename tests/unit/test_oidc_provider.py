"""Real signature/claim/JWKS rotation tests; run with the oidc extra."""
import json
import time
from unittest.mock import patch

import pytest
from celery import Celery
from fastapi.testclient import TestClient

jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")

from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.providers import OIDCBearerProvider


def make_key(kid):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid=kid, use="sig", alg="RS256")
    return private, jwk


@pytest.fixture
def env():
    key, jwk = make_key("one")
    provider = OIDCBearerProvider(issuer="https://identity.example", audience="manager", jwks_url="https://identity.example/keys",
                                  role_map={"Ops.Admin": "admin", "Ops.Read": "viewer"}, leeway_s=0)
    config = CeleryManagerConfig(celery_app=Celery("oidc"), db_url="sqlite:///:memory:", secret_key="s",
                                local_login_enabled=False, principal_providers=(provider,))
    app, _ = create_app(config)
    client = TestClient(app, base_url="https://testserver")
    def token(**changes):
        claims = {"iss": provider.issuer, "aud": provider.audience, "sub": "person-1", "exp": int(time.time()) + 60,
                  "roles": ["Ops.Read", "Ops.Admin"]}
        claims.update(changes)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "one"})
    with patch.object(jwt.PyJWKClient, "fetch_data", return_value={"keys": [jwk]}):
        yield provider, client, token


def test_valid_signature_maps_highest_explicit_role_and_stable_subject(env):
    _, client, token = env
    response = client.get("/api/whoami", headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert response.json()["subject"] == "person-1"


@pytest.mark.parametrize("changes", [{"aud": "wrong"}, {"iss": "https://wrong.example"}, {"exp": 1}])
def test_wrong_claims_fail_closed(env, changes):
    _, client, token = env
    assert client.get("/api/whoami", headers={"Authorization": "Bearer " + token(**changes)}).status_code == 401


def test_invalid_signature_and_unsigned_token_fail_closed(env):
    _, client, token = env
    encoded = token()
    header, payload, signature = encoded.split(".")
    signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert client.get("/api/whoami", headers={"Authorization": f"Bearer {header}.{payload}.{signature}"}).status_code == 401
    unsigned = jwt.encode({"sub": "x", "exp": int(time.time()) + 60}, key="", algorithm="none")
    assert client.get("/api/whoami", headers={"Authorization": "Bearer " + unsigned}).status_code == 401


def test_no_explicit_role_mapping_has_no_access(env):
    _, client, token = env
    assert client.get("/api/whoami", headers={"Authorization": "Bearer " + token(roles=["admin"])}).status_code == 403


def test_key_rotation_by_kid_and_custom_mapper(env):
    provider, client, _ = env
    key, jwk = make_key("two")
    provider.role_mapper = lambda claims: "operator" if claims.get("permission") == "run" else None
    encoded = jwt.encode({"iss": provider.issuer, "aud": "manager", "sub": "second", "permission": "run",
                          "exp": int(time.time()) + 60}, key, algorithm="RS256", headers={"kid": "two"})
    with patch.object(jwt.PyJWKClient, "fetch_data", return_value={"keys": [jwk]}):
        assert client.get("/api/whoami", headers={"Authorization": "Bearer " + encoded}).json()["role"] == "operator"


@pytest.mark.parametrize("changes, status", [
    ({"roles": None}, 403), ({"roles": 12}, 401), ({"roles": {}}, 401),
    ({"preferred_username": ["invalid"]}, 401), ({"preferred_username": "   "}, 401),
])
def test_untrusted_claim_shapes_are_auth_responses_not_server_errors(env, changes, status):
    _, client, token = env
    assert client.get("/api/whoami", headers={"Authorization": "Bearer " + token(**changes)}).status_code == status
