"""Built-in OIDC sign-in: authorization code with PKCE, then a tracked session.

The provider (an ``OIDCBearerProvider`` in ``principal_providers``) supplies
issuer, audience, algorithms and the role map; the access token it issues is
validated exactly like a bearer token. The session row keeps the refresh token
and the role last read with it; every ``oidc_role_refresh_seconds`` the role is
read again, and a failed refresh ends the session.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import HTTPException
from itsdangerous import BadData, URLSafeTimedSerializer

from celery_manager.auth.state import now, session_hash, utc
from celery_manager.db.auth_state import BrowserSession

STATE_COOKIE = "cm_oidc_state"
STATE_MAX_AGE = 600
_STATE_SALT = "celery-manager-oidc-state"


def find_provider(config):
    return next((p for p in config.principal_providers if hasattr(p, "validate") and hasattr(p, "discovery")), None)


def redirect_uri(config, request) -> str:
    base = (config.dashboard_url or str(request.base_url)).rstrip("/")
    return base + config.oidc_redirect_path


def start(config, request):
    """``(authorization_url, signed_state_cookie)``."""
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({"response_type": "code", "client_id": config.oidc_client_id,
                       "redirect_uri": redirect_uri(config, request), "scope": " ".join(config.oidc_scopes),
                       "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    endpoint = find_provider(config).discovery()["authorization_endpoint"]
    url = endpoint + ("&" if "?" in endpoint else "?") + query
    cookie = URLSafeTimedSerializer(config.secret_key, salt=_STATE_SALT).dumps({"state": state, "verifier": verifier})
    return url, cookie


def read_state(config, cookie, state):
    """The PKCE verifier when ``state`` matches the signed cookie, else None."""
    try:
        data = URLSafeTimedSerializer(config.secret_key, salt=_STATE_SALT).loads(cookie or "", max_age=STATE_MAX_AGE)
    except BadData:
        return None
    if not state or not secrets.compare_digest(str(data.get("state")), state):
        return None
    return data.get("verifier")


def _token_request(config, provider, data):
    auth = None
    data = {**data, "client_id": config.oidc_client_id}
    if config.oidc_client_secret:
        auth = (config.oidc_client_id, config.oidc_client_secret)
    return provider.post_form(provider.discovery()["token_endpoint"], data, auth)


def exchange_code(config, request, code, verifier):
    """``(ExternalUser, refresh_token)`` for an authorization code; HTTP 401 on any failure."""
    provider = find_provider(config)
    try:
        tokens = _token_request(config, provider, {"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": redirect_uri(config, request),
                                                   "code_verifier": verifier})
        access = tokens["access_token"]
    except Exception as exc:
        raise HTTPException(401, "The identity provider refused the sign-in") from exc
    return provider.validate(access), tokens.get("refresh_token")


def refresh_role(config, db_session, row) -> str | None:
    """Re-read the role with the stored refresh token; None when the session must end."""
    provider = find_provider(config)
    if not row.refresh_token:
        return None
    try:
        tokens = _token_request(config, provider, {"grant_type": "refresh_token", "refresh_token": row.refresh_token})
        user = provider.validate(tokens["access_token"])
    except Exception:  # noqa: BLE001 - any failure ends the session
        return None
    if user.subject != row.subject or user.role is None:
        return None
    row.refresh_token = tokens.get("refresh_token") or row.refresh_token
    row.role, row.roles_checked_at = user.role, now()
    db_session.commit()
    return user.role


def session_role(config, db_session, sid, username, subject) -> str | None:
    """The role of a live OIDC session, refreshed when due; None when unusable."""
    row = db_session.get(BrowserSession, session_hash(sid))
    if (row is None or row.username != username or row.subject != subject or row.revoked_at is not None
            or utc(row.expires_at) <= now() or row.role is None):
        return None
    if utc(row.roles_checked_at) + timedelta(seconds=config.oidc_role_refresh_seconds) <= now():
        role = refresh_role(config, db_session, row)
        if role is None:
            row.revoked_at, row.refresh_token = now(), None
            db_session.commit()
        return role
    return row.role
