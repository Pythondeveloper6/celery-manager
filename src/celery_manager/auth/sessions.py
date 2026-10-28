from __future__ import annotations

from itsdangerous import BadData, URLSafeTimedSerializer

_SALT = "celery-manager-session"


def sign_session(secret_key: str, username: str, fingerprint: str = "") -> str:
    """``fingerprint`` is `auth.users.session_fingerprint` of the account's
    password hash; the API refuses a cookie whose fingerprint no longer
    matches, which is how a password change signs everyone out."""
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    return serializer.dumps({"username": username, "pv": fingerprint})


def verify_session_claims(
    secret_key: str, token: str, max_age_seconds: int = 60 * 60 * 24 * 7
) -> tuple[str, str] | None:
    """``(username, fingerprint)`` from a valid token, else None. A token
    issued before fingerprints existed carries an empty one, which never
    matches, so it is simply treated as a sign-out."""
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    try:
        data = serializer.loads(token, max_age=max_age_seconds)
    except BadData:
        return None
    username = data.get("username") if isinstance(data, dict) else None
    if not isinstance(username, str):
        return None
    fingerprint = data.get("pv")
    return username, fingerprint if isinstance(fingerprint, str) else ""


def verify_session(secret_key: str, token: str, max_age_seconds: int = 60 * 60 * 24 * 7) -> str | None:
    claims = verify_session_claims(secret_key, token, max_age_seconds)
    return None if claims is None else claims[0]
