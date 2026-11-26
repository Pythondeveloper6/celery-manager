"""Create, check and revoke scoped API tokens. Only a SHA-256 of each token
is stored (tokens are 256 random bits, so a fast hash is enough); the token
itself is shown once, at creation."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from celery_manager.db.api_tokens import ApiToken

PREFIX = "cmk_"
LAST_USED_RESOLUTION = timedelta(minutes=1)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(session, name: str, role: str, owner: str, expires_at: datetime | None = None) -> tuple[ApiToken, str]:
    from celery_manager.auth.roles import validate_role
    validate_role(role)
    if not name or len(name) > 64:
        raise ValueError("name must be 1..64 characters")
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")
    token = PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(name=name, token_hash=_hash(token), role=role, owner=owner,
                   created_at=datetime.now(timezone.utc), expires_at=expires_at)
    session.add(row)
    session.commit()
    return row, token


def authenticate(session, token: str) -> ApiToken | None:
    """The live token row for `token`, or None. Records the use (at most
    once a minute per token)."""
    if not token.startswith(PREFIX):
        return None
    row = session.query(ApiToken).filter_by(token_hash=_hash(token)).one_or_none()
    now = datetime.now(timezone.utc)

    def aware(value):
        return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)
    if row is None or row.revoked_at is not None or (row.expires_at is not None and aware(row.expires_at) <= now):
        return None
    if row.last_used_at is None or now - aware(row.last_used_at) >= LAST_USED_RESOLUTION:
        row.last_used_at = now
        session.commit()
    return row


def revoke_token(session, token_id: int) -> ApiToken | None:
    row = session.get(ApiToken, token_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        session.commit()
    return row
