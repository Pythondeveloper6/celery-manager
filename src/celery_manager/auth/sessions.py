from __future__ import annotations

from itsdangerous import BadData, URLSafeTimedSerializer

_SALT = "celery-manager-session"


def sign_session(secret_key: str, username: str, fingerprint: str = "", *, session_id: str | None = None) -> str:
    """``fingerprint`` is `auth.users.session_fingerprint` of the account's
    password hash; the API refuses a cookie whose fingerprint no longer
    matches, which is how a password change signs everyone out."""
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    return serializer.dumps({"username": username, "pv": fingerprint, **({"sid": session_id} if session_id else {})})


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


def sign_bridge(secret_key, username, role, subject=None, *, session_id=None):
    return URLSafeTimedSerializer(secret_key, salt="celery-manager-bridge").dumps(
        {"username": username, "role": role, "subject": subject, **({"sid": session_id} if session_id else {})})


def verify_bridge(secret_key, token, max_age):
    from celery_manager.auth.roles import ROLES
    try:
        data = URLSafeTimedSerializer(secret_key, salt="celery-manager-bridge").loads(token, max_age=max_age)
    except BadData:
        return None
    if (not isinstance(data, dict) or data.get("role") not in ROLES
            or not isinstance(data.get("username"), str)):
        return None
    return {k: data.get(k) for k in ("username", "role", "subject")}


def browser_session_id(secret_key, token, max_age, *, bridge=False):
    """Return a signed bearer ID, or None for old stateless cookies.

    This helper does not authenticate by itself: verify claims and account
    fingerprint before accepting a local session, then check DB revocation.
    """
    salt = "celery-manager-bridge" if bridge else _SALT
    try:
        data = URLSafeTimedSerializer(secret_key, salt=salt).loads(token, max_age=max_age)
    except BadData:
        return None
    value = data.get("sid") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None
