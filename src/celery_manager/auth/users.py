from __future__ import annotations

import hashlib
import hmac
import re

import bcrypt

from celery_manager.auth.roles import ADMIN, validate_role
from celery_manager.db.models import User

# bcrypt only ever looks at the first 72 bytes of its input, and bcrypt>=5
# raises ValueError rather than silently truncating. Enforced here so an
# over-long password is a clean rejection, not an unhandled 500 at login.
MAX_PASSWORD_BYTES = 72

# Minimum length for a password set through the dashboard or any other
# user-management entry point (control/users.py). Not applied by
# `create_user()` itself or the CLI: both predate it, scripts and tests call
# them with short throwaway passwords, and raising their bar would break
# setups that work today. Counted in characters, not bytes.
MIN_PASSWORD_LENGTH = 8

# Usernames the dashboard will create. Deliberately narrower than the
# `users.username` column: they appear in URL paths (`/api/users/{username}`)
# and the audit log, so no slashes, spaces or control characters. Email
# addresses are fine.
MAX_USERNAME_LENGTH = 64
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]*$")

# Hash of a throwaway value, at the same cost factor `bcrypt.gensalt()` uses
# for real accounts (12). Checked against whenever there is no real hash to
# check, so a login for a username that doesn't exist costs the same bcrypt
# round as one for a username that does — otherwise response time alone tells
# an attacker which usernames are real. A literal rather than computed at
# import so importing this module doesn't pay for a hash.
_DUMMY_HASH = b"$2b$12$ysd1eu9/3zS5P2WLJmNh0uTcSzFBUvlXuzcNVUY/x6F/VoRhuJA72"


def validate_username(username: str) -> str:
    """Return ``username`` if the dashboard may create it, else raise ValueError."""
    if not username or len(username) > MAX_USERNAME_LENGTH or not _USERNAME.match(username):
        raise ValueError(
            f"username must be 1-{MAX_USERNAME_LENGTH} characters: letters, digits and . _ @ + -, "
            "starting with a letter or digit"
        )
    return username


def validate_password(password: str, *, enforce_minimum: bool = False) -> bytes:
    """The UTF-8 bytes bcrypt will hash, or ValueError if the password is
    unacceptable. ``enforce_minimum`` adds the :data:`MIN_PASSWORD_LENGTH`
    check that dashboard user management applies; see that constant for why
    the lower-level callers don't."""
    encoded = password.encode("utf-8")
    if not encoded:
        raise ValueError("password must not be empty")
    if enforce_minimum and len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(encoded) > MAX_PASSWORD_BYTES:
        # Refused rather than truncated: a user who sets a 100-byte password
        # would otherwise be able to log in with only its first 72 bytes.
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded")
    return encoded


def hash_password(password: str, *, enforce_minimum: bool = False) -> str:
    """bcrypt hash of ``password``; see :func:`validate_password`."""
    encoded = validate_password(password, enforce_minimum=enforce_minimum)
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def session_fingerprint(secret_key: str, password_hash: str) -> str:
    """A value that changes whenever the account's password does.

    Signed into the session cookie and compared on every request, so setting
    a new password (bcrypt salts every hash, so even the same password gives
    a new one) ends every session issued before it, with no server-side
    session store. An HMAC under the secret key rather than a bare digest:
    the cookie payload is signed but readable, and this keeps anything
    derived from the stored hash out of it.
    """
    return hmac.new(secret_key.encode("utf-8"), password_hash.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def create_user(session, username: str, password: str, role: str = ADMIN) -> User:
    """Create a dashboard account.

    ``role`` defaults to ``admin`` so code written before roles existed
    (``create_user(session, name, pw)`` in setup scripts and examples) keeps
    creating accounts that can do what every account could do then. The
    ``celery-manager create-user`` CLI defaults to ``viewer`` instead; see
    docs/roles.md.
    """
    validate_role(role)
    password_hash = hash_password(password)
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


def get_user_auth(session, username: str) -> tuple[str, str] | None:
    """``(role, password_hash)`` for the account, or None if there is none.
    What every authenticated request needs: the role to authorise with, and
    the hash to check the session's fingerprint against."""
    row = session.query(User.role, User.password_hash).filter_by(username=username).one_or_none()
    return None if row is None else (row[0], row[1])


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
