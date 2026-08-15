from __future__ import annotations

from itsdangerous import BadData, URLSafeTimedSerializer

_SALT = "celery-manager-session"


def sign_session(secret_key: str, username: str) -> str:
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    return serializer.dumps({"username": username})


def verify_session(secret_key: str, token: str, max_age_seconds: int = 60 * 60 * 24 * 7) -> str | None:
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    try:
        data = serializer.loads(token, max_age=max_age_seconds)
    except BadData:
        return None
    return data.get("username")
