"""Dashboard user management: list, add, change role, reset password, delete.

The same operations the ``celery-manager`` CLI offers, plus the safety rules
that only matter once several people can do this from a browser at once. The
rules live here, not in the routes, so Python callers get them too:

- There is always at least one admin. The last admin can't be demoted or
  deleted, so nobody can lock the deployment out of its own user management
  (the CLI would still get you back in, but it needs shell access).
- Nobody deletes their own account from the dashboard. Deleting yourself
  would sign you out mid-request, and the only case that is ever wanted
  (leaving the team) is as well served by another admin doing it. Another
  admin can delete you as long as it isn't the last admin.
- An admin doesn't reset their own password through the admin path; they use
  :func:`change_own_password`, which asks for the current one.

Every change writes an audit row. The password is never part of one, nor of
any exception message.

Errors: ValueError for bad input, LookupError for an unknown account,
:class:`UserConflict` for a request that is valid but refused by one of the
rules above (or a duplicate username), PermissionError for a wrong current
password.
"""

from __future__ import annotations

import secrets

from celery_manager.auth.external import SOURCE_LOCAL
from celery_manager.auth.roles import ADMIN, validate_role
from celery_manager.auth.users import (
    create_user,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)
from celery_manager.control.audit import record as record_audit
from celery_manager.db.models import User


class UserConflict(Exception):
    """The request is well-formed but the current state refuses it."""


# `source` says where an account comes from. Everything listed here is stored
# in this deployment's own users table ("local"). Users signed in through the
# host app (`CeleryManagerConfig.authenticate_request`) live in the host's
# database and get their role from it, so there is nothing here to list or
# manage for them.
def _entry(username: str, role: str) -> dict:
    return {"username": username, "role": role, "source": SOURCE_LOCAL}


def _get(session, username: str) -> User:
    user = session.query(User).filter_by(username=username).one_or_none()
    if user is None:
        raise LookupError(f"no dashboard user named {username!r}")
    return user


def _lock_admins(session) -> list[User]:
    """Every admin, row-locked on databases that support it, so two admins
    demoting each other at the same moment can't both see "another admin
    exists" and leave none. (SQLite serialises writers anyway.)"""
    return session.query(User).filter_by(role=ADMIN).with_for_update().all()


def list_users(session) -> list[dict]:
    rows = session.query(User.username, User.role).order_by(User.username).all()
    return [_entry(name, role) for name, role in rows]


def add_user(session, actor: str, username: str, password: str, role: str) -> dict:
    validate_username(username)
    validate_role(role)
    validate_password(password, enforce_minimum=True)  # report a bad password before a duplicate name
    if session.query(User.id).filter_by(username=username).first() is not None:
        raise UserConflict(f"a dashboard user named {username!r} already exists")
    create_user(session, username, password, role=role)
    record_audit(session, actor, "create_user", "user", username, {"role": role})
    return _entry(username, role)


def change_role(session, actor: str, username: str, role: str) -> dict:
    validate_role(role)
    user = _get(session, username)
    old_role = user.role
    if old_role == role:
        return _entry(username, role)
    if old_role == ADMIN and len(_lock_admins(session)) <= 1:
        raise UserConflict(
            f"{username!r} is the only admin. Make another account an admin before changing this one's role."
        )
    user.role = role
    session.commit()
    record_audit(session, actor, "set_role", "user", username, {"from": old_role, "to": role})
    return _entry(username, role)


def generate_password() -> str:
    """A random password, 16 URL-safe characters (~96 bits)."""
    return secrets.token_urlsafe(12)


def reset_password(
    session, actor: str, username: str, password: str | None = None, *, generate: bool = False
) -> str | None:
    """Set a new password for ``username``, ending all of their sessions.

    Pass ``password`` or ``generate=True``, not both. Returns the generated
    password (the only time it is ever visible) or None when one was given.
    """
    if generate == (password is not None):
        raise ValueError("provide either a new password or generate=true, not both")
    if actor == username:
        raise UserConflict("Use 'Change password' to change your own password; it asks for the current one.")
    user = _get(session, username)
    new_password = generate_password() if generate else password
    user.password_hash = hash_password(new_password, enforce_minimum=True)
    session.commit()
    record_audit(session, actor, "reset_password", "user", username, {"generated": generate})
    return new_password if generate else None


def change_own_password(session, username: str, current_password: str, new_password: str) -> User:
    """Any account changing its own password. Needs the current one, so a
    borrowed or stolen session can't be turned into a permanent takeover."""
    if verify_password(session, username, current_password) is None:
        raise PermissionError("current password is incorrect")
    user = _get(session, username)
    user.password_hash = hash_password(new_password, enforce_minimum=True)
    session.commit()
    record_audit(session, username, "change_password", "user", username)
    return user


def delete_user(session, actor: str, username: str) -> None:
    if actor == username:
        raise UserConflict("You can't delete your own account. Ask another admin to do it.")
    user = _get(session, username)
    if user.role == ADMIN and len(_lock_admins(session)) <= 1:
        raise UserConflict(f"{username!r} is the only admin and can't be deleted.")
    role = user.role
    session.delete(user)
    session.commit()
    record_audit(session, actor, "delete_user", "user", username, {"role": role})
