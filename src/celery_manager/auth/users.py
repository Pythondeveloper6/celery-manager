from __future__ import annotations

import bcrypt

from celery_manager.auth.roles import ADMIN, validate_role
from celery_manager.db.models import User

# bcrypt only ever looks at the first 72 bytes of its input, and bcrypt>=5
# raises ValueError rather than silently truncating. Enforced here so an
# over-long password is a clean rejection, not an unhandled 500 at login.
MAX_PASSWORD_BYTES = 72

# Hash of a throwaway value, at the same cost factor `bcrypt.gensalt()` uses
# for real accounts (12). Checked against whenever there is no real hash to
# check, so a login for a username that doesn't exist costs the same bcrypt
# round as one for a username that does — otherwise response time alone tells
# an attacker which usernames are real. A literal rather than computed at
# import so importing this module doesn't pay for a hash.
_DUMMY_HASH = b"$2b$12$ysd1eu9/3zS5P2WLJmNh0uTcSzFBUvlXuzcNVUY/x6F/VoRhuJA72"


def create_user(session, username: str, password: str, role: str = ADMIN) -> User:
    """Create a dashboard account.

    ``role`` defaults to ``admin`` so code written before roles existed
    (``create_user(session, name, pw)`` in setup scripts and examples) keeps
    creating accounts that can do what every account could do then. The
    ``celery-manager create-user`` CLI defaults to ``viewer`` instead; see
    docs/roles.md.
    """
    validate_role(role)
    encoded = password.encode("utf-8")
    if not encoded:
        raise ValueError("password must not be empty")
    if len(encoded) > MAX_PASSWORD_BYTES:
        # Refused rather than truncated: a user who sets a 100-byte password
        # would otherwise be able to log in with only its first 72 bytes.
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded")
    password_hash = bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")
    user = User(username=username, password_hash=password_hash, role=role)
    session.add(user)
    session.commit()
    return user


def user_exists(session, username: str) -> bool:
    return session.query(User.id).filter_by(username=username).first() is not None


def get_user_role(session, username: str) -> str | None:
    """The account's role, or None if there is no such account."""
    row = session.query(User.role).filter_by(username=username).one_or_none()
    return None if row is None else row[0]


def set_user_role(session, username: str, role: str) -> User:
    """Change an existing account's role. Takes effect on the account's next
    request: the role is read from the database every time, not baked into
    the session cookie. Raises LookupError if there is no such account."""
    validate_role(role)
    user = session.query(User).filter_by(username=username).one_or_none()
    if user is None:
        raise LookupError(f"no dashboard user named {username!r}")
    user.role = role
    session.commit()
    return user


def verify_password(session, username: str, password: str) -> User | None:
    encoded = password.encode("utf-8")
    user = session.query(User).filter_by(username=username).one_or_none()
    if user is None or len(encoded) > MAX_PASSWORD_BYTES:
        # Spend the same bcrypt round a real check would (see _DUMMY_HASH).
        # An over-long password can never match — create_user refuses them —
        # so it takes this path too rather than reaching bcrypt's ValueError.
        bcrypt.checkpw(b"not-the-password", _DUMMY_HASH)
        return None
    if bcrypt.checkpw(encoded, user.password_hash.encode("utf-8")):
        return user
    return None
