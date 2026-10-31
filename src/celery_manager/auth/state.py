"""Database-backed session revocation and login admission control."""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError

from celery_manager.db.auth_state import BrowserSession, LoginBackoff


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def now():
    return datetime.now(timezone.utc)


def session_hash(session_id):
    return hashlib.sha256(session_id.encode()).hexdigest()


def cleanup(session, config):
    """Bound each opportunistic sweep, with indexed expiry predicates."""
    at = now()
    expired = list(session.scalars(select(BrowserSession.session_id).where(BrowserSession.expires_at <= at).limit(1000)))
    if expired:
        session.execute(delete(BrowserSession).where(BrowserSession.session_id.in_(expired)))
    stale = list(session.scalars(select(LoginBackoff.key).where(
        LoginBackoff.updated_at < at - timedelta(seconds=config.login_backoff_reset_seconds + config.login_backoff_max_seconds)
    ).limit(1000)))
    if stale:
        session.execute(delete(LoginBackoff).where(
            LoginBackoff.key.in_(stale),
            LoginBackoff.updated_at < at - timedelta(seconds=config.login_backoff_reset_seconds + config.login_backoff_max_seconds)))


def create_session(session, config, username, source, subject=None, *, lifetime=None):
    cleanup(session, config)
    session_id = secrets.token_urlsafe(32)
    at = now()
    session.add(BrowserSession(session_id=session_hash(session_id), username=username, source=source,
                               subject=subject, created_at=at,
                               expires_at=at + timedelta(seconds=lifetime or config.session_max_age_seconds)))
    session.commit()
    return session_id


def active_session(session, session_id, username, source, subject=None):
    row = session.get(BrowserSession, session_hash(session_id))
    return (row is not None and row.username == username and row.source == source and row.subject == subject
            and row.revoked_at is None and utc(row.expires_at) > now())


def revoke_session(session, identifier, principal):
    # identifier is a public hash, never the cookie's bearer ID.
    result = session.execute(
        BrowserSession.__table__.update().where(
            BrowserSession.session_id == identifier, BrowserSession.username == principal.username,
            BrowserSession.source == principal.source, BrowserSession.subject == principal.subject,
            BrowserSession.revoked_at.is_(None)
        ).values(revoked_at=now())
    )
    session.commit()
    return bool(result.rowcount)


def _locked_backoff(session, key):
    values = {"key": key, "attempts": 0, "updated_at": now()}
    dialect = session.get_bind().dialect.name
    if dialect in ("sqlite", "postgresql"):
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        session.execute(dialect_insert(LoginBackoff).values(**values).on_conflict_do_nothing(index_elements=["key"]))
    elif dialect in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import insert as dialect_insert
        stmt = dialect_insert(LoginBackoff).values(**values)
        session.execute(stmt.on_duplicate_key_update(key=stmt.inserted.key))
    else:
        # SQL Server locks the absent-key range before inserting. Other
        # dialects use a savepoint to tolerate a concurrent first sighting.
        # populate_existing: admit_login's unlocked pre-read may already hold
        # this row in the identity map. Without a refresh the locked read
        # would return those stale attributes and concurrent reservations
        # would overwrite each other's increments.
        query = select(LoginBackoff).where(LoginBackoff.key == key).with_for_update().with_hint(
            LoginBackoff, "WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql").execution_options(populate_existing=True)
        row = session.scalar(query)
        if row is not None:
            return row
        try:
            with session.begin_nested():
                session.execute(insert(LoginBackoff).values(**values))
        except IntegrityError:
            pass
    return session.scalar(select(LoginBackoff).where(LoginBackoff.key == key).with_for_update().with_hint(
        LoginBackoff, "WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql").execution_options(populate_existing=True))


def admit_login(session, config, username, client_host):
    """Reserve an attempt atomically before bcrypt; return Retry-After or zero.

    Account and direct peer have separate budgets. Forwarded headers are
    never trusted implicitly. This storage is shared by all API processes.
    """
    cleanup(session, config)
    keys = [hmac.new(config.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()
            for value in ("account:" + username, "peer:" + client_host)]
    at, rows = now(), []
    # An already denied peer/account must not create an unlimited number of
    # counterpart rows as callers rotate usernames or client addresses. This
    # read only rejects; admission still rechecks under the sorted row locks.
    existing = session.scalars(select(LoginBackoff).where(LoginBackoff.key.in_(keys))).all()
    wait = _backoff_wait(existing, at, config)
    if wait > 0:
        session.commit()
        return wait, keys
    # Stable order prevents two-key deadlocks on supporting SQL backends.
    for key in sorted(set(keys)):
        row = _locked_backoff(session, key)
        if utc(row.updated_at) < at - timedelta(seconds=config.login_backoff_reset_seconds):
            row.attempts, row.blocked_until = 0, None
        rows.append(row)
    wait = _backoff_wait(rows, at, config)
    if wait > 0:
        session.commit()
        return wait, keys
    for row in rows:
        row.attempts += 1
        row.updated_at = at
        if row.attempts >= config.login_backoff_threshold:
            delay = min(config.login_backoff_max_seconds, 2 ** min(row.attempts - config.login_backoff_threshold, 30))
            row.blocked_until = at + timedelta(seconds=delay)
    session.commit()
    return 0, keys


def login_succeeded(session, keys, config):
    # Match admission's global lock order. Preserve the account row so a
    # waiting reservation cannot fall into a delete/recreate race.
    rows = {key: _locked_backoff(session, key) for key in sorted(set(keys))}
    account, peer = rows[keys[0]], rows[keys[1]]
    account.attempts, account.blocked_until = 0, None
    account.updated_at = now()
    peer.attempts = max(0, peer.attempts - 1)
    if peer.attempts < config.login_backoff_threshold:
        peer.blocked_until = None
    session.commit()


def _backoff_wait(rows, at, config):
    reset_before = at - timedelta(seconds=config.login_backoff_reset_seconds)
    return max((math.ceil((utc(row.blocked_until) - at).total_seconds())
                for row in rows if row.blocked_until is not None and utc(row.updated_at) >= reset_before), default=0)
