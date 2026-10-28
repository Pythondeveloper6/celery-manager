"""Optional request authentication adapters. Dependencies are imported lazily."""
from __future__ import annotations

import json
from typing import Protocol
from urllib.request import urlopen

from fastapi import HTTPException

from celery_manager.auth.external import ExternalUser
from celery_manager.auth.roles import ROLES


class PrincipalProvider(Protocol):
    def authenticate(self, request) -> ExternalUser | None: ...


class CallableProvider:
    def __init__(self, fn):
        self.fn = fn

    def authenticate(self, request):
        return self.fn(request)


class OIDCBearerProvider:
    """JWT/JWKS validation with explicit issuer, audience and asymmetric algorithms.

    Supply role_map or role_mapper; no token role is granted implicitly.
    Callable on its own for compatibility with authenticate_request.
    """

    def __init__(self, *, issuer, audience, jwks_url=None, algorithms=("RS256",),
                 leeway_s=60, jwks_cache_s=3600, username_claim="preferred_username",
                 subject_claim="sub", role_claim="roles", role_map=None, role_mapper=None):
        if not issuer.startswith("https://") or (jwks_url and not jwks_url.startswith("https://")):
            raise ValueError("OIDC issuer and JWKS URL must use HTTPS")
        if not audience or not algorithms or any(a not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"} for a in algorithms):
            raise ValueError("Configure an audience and asymmetric JWT algorithms")
        if role_map and any(role not in ROLES for role in role_map.values()):
            raise ValueError("role_map must map to viewer, operator or admin")
        self.issuer, self.audience, self.jwks_url = issuer, audience, jwks_url
        self.algorithms, self.leeway = tuple(algorithms), leeway_s
        self.cache_s = jwks_cache_s
        self.username_claim, self.subject_claim = username_claim, subject_claim
        self.role_claim, self.role_map, self.role_mapper = role_claim, role_map or {}, role_mapper
        self._client = None

    def _jwks_client(self, jwt):
        if self._client is None:
            url = self.jwks_url
            if url is None:
                with urlopen(self.issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=10) as response:
                    discovery = json.load(response)
                if discovery.get("issuer") != self.issuer:
                    raise ValueError("Discovery issuer mismatch")
                url = discovery["jwks_uri"]
                if not url.startswith("https://"):
                    raise ValueError("JWKS URL must use HTTPS")
            self._client = jwt.PyJWKClient(url, lifespan=self.cache_s, timeout=10)
        return self._client

    def authenticate(self, request):
        authorization = request.headers.get("authorization")
        if authorization is None:
            return None
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(401, "Expected a bearer token")
        try:
            import jwt
        except ImportError as exc:
            raise RuntimeError("Install celery-manager[oidc] to use OIDC authentication") from exc
        try:
            key = self._jwks_client(jwt).get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=self.algorithms,
                                issuer=self.issuer, audience=self.audience, leeway=self.leeway,
                                options={"require": ["exp", "iss", "aud", self.subject_claim]})
        except Exception as exc:
            raise HTTPException(401, "Invalid or expired bearer token") from exc
        subject = claims[self.subject_claim]
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise HTTPException(401, "Invalid token subject")
        if self.role_mapper:
            role = self.role_mapper(claims)
        else:
            values = claims.get(self.role_claim, []) if self.role_claim else []
            if isinstance(values, str):
                values = [values]
            mapped = {self.role_map[v] for v in values if isinstance(v, str) and v in self.role_map}
            role = next((r for r in reversed(ROLES) if r in mapped), None)
        return ExternalUser(claims.get(self.username_claim) or subject, role, subject)

    __call__ = authenticate
