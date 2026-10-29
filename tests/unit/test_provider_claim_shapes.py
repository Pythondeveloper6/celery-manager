"""Untrusted decoded claim handling, independent of optional crypto installation."""
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from celery_manager.auth.providers import OIDCBearerProvider


@pytest.fixture
def authenticate(monkeypatch):
    def call(**changes):
        claims = {"sub": "subject", "roles": ["reader"]}
        claims.update(changes)
        jwt = SimpleNamespace(decode=lambda *args, **kwargs: claims)
        monkeypatch.setitem(sys.modules, "jwt", jwt)
        provider = OIDCBearerProvider(issuer="https://identity.example", audience="manager", role_map={"reader": "viewer"})
        monkeypatch.setattr(provider, "_jwks_client", lambda _: SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key="test")))
        return provider.authenticate(SimpleNamespace(headers={"authorization": "Bearer test"}))
    return call


def test_null_or_missing_role_assignment_has_no_role(authenticate):
    assert authenticate(roles=None).role is None
    assert authenticate(roles="reader").role == "viewer"
    assert authenticate(roles=[{}, 1, "reader"]).role == "viewer"


@pytest.mark.parametrize("roles", [12, True, {"reader": "admin"}])
def test_non_sequence_role_claim_fails_cleanly(authenticate, roles):
    with pytest.raises(HTTPException) as exc:
        authenticate(roles=roles)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("claims", [{"sub": "bad\nsubject"}, {"preferred_username": ["name"]},
                                  {"preferred_username": "bad\nname"}, {"preferred_username": "x" * 256}, {"preferred_username": "   "}])
def test_untrusted_identity_claims_fail_cleanly(authenticate, claims):
    with pytest.raises(HTTPException) as exc:
        authenticate(**claims)
    assert exc.value.status_code == 401
