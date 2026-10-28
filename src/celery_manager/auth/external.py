"""Signing in with the host application's own users.

When celery-manager is mounted inside (or next to) a Django, Flask or FastAPI
app, ``CeleryManagerConfig.authenticate_request`` lets that app say who the
request belongs to and which celery-manager role they get, instead of keeping
a second set of accounts. See docs/integrations.md for the ready-made Django
and Flask helpers (``celery_manager.integrations``).

The contract is deliberately small and fails closed:

- The authenticator gets the Starlette ``Request`` and returns an
  :class:`ExternalUser`, or ``None`` for "this request isn't signed in to the
  host". ``None`` is the only answer that lets celery-manager fall back to
  its own ``cm_session`` accounts.
- ``ExternalUser.role`` is one of the roles in ``auth.roles``, or ``None`` for
  "a real host user who gets no access", which is a 403, never a fallback: a
  host user who has been refused must not be able to get in some other way
  through the same browser.
- Anything else (an unknown role, a wrong return type, an exception from the
  host's code) is a 500 and is logged. A misconfigured mapping must never be
  guessed upwards into access, nor silently downwards into "not signed in",
  which would hide the bug behind the login page.
- It is called on every request. The role is the host's answer *now*, so a
  demotion or a removed user takes effect on the next request, the same
  promise local accounts make.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass

import anyio.from_thread
from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

from celery_manager.auth.roles import ROLES

logger = logging.getLogger(__name__)

# Where the signed-in account comes from; also the `source` in `GET
# /api/whoami` and `GET /api/users` entries.
SOURCE_LOCAL = "local"
SOURCE_EXTERNAL = "external"

# The audit log's `username` column is String(255); a longer name would be
# cut (or refused) at the first state-changing request instead of at sign-in.
MAX_EXTERNAL_USERNAME_LENGTH = 255


@dataclass(frozen=True)
class ExternalUser:
    """Who the host application says the request belongs to.

    username: shown in the dashboard and recorded as the actor in the audit
        log. Use the identifier people know each other by in the host app
        (Django's ``get_username()``, an email address).
    role: ``"viewer"``, ``"operator"``, ``"admin"``, or ``None`` when this
        host user may not use the dashboard at all (they get a 403 page).
    """

    username: str
    role: str | None


class ExternalAuthMisconfigured(Exception):
    """The host's authenticator answered something celery-manager can't use."""


def _check(result: object) -> ExternalUser | None:
    if result is None:
        return None
    if not isinstance(result, ExternalUser):
        raise ExternalAuthMisconfigured(
            f"authenticate_request returned {type(result).__name__}; expected ExternalUser or None"
        )
    username = result.username
    if (
        not isinstance(username, str)
        or not username.strip()
        or len(username) > MAX_EXTERNAL_USERNAME_LENGTH
        # Control characters would let a host username forge line breaks in
        # logs and the audit export.
        or not username.isprintable()
    ):
        raise ExternalAuthMisconfigured(
            f"authenticate_request returned an unusable username {username!r}: it must be 1-"
            f"{MAX_EXTERNAL_USERNAME_LENGTH} printable characters"
        )
    if result.role is not None and result.role not in ROLES:
        raise ExternalAuthMisconfigured(
            f"authenticate_request returned role {result.role!r} for {username!r}; "
            f"expected one of {', '.join(ROLES)}, or None for no access"
        )
    return result


async def _await(awaitable):
    return await awaitable


def authenticate_external(config, request) -> ExternalUser | None:
    """Ask the host who ``request`` belongs to. ``None`` when no authenticator
    is configured or the host says nobody is signed in.

    Called from a sync dependency, which FastAPI runs in its threadpool: a
    sync authenticator may block (an ORM query) without stalling the event
    loop, and an ``async def`` one is awaited back on the loop that owns the
    request.

    Raises HTTPException: whatever the authenticator raised itself (a host
    dependency reused as-is may raise its own 401), a 500 for a misconfigured
    answer or a crash, never a quiet ``None``.
    """
    authenticate = config.authenticate_request
    if authenticate is None:
        return None
    try:
        result = authenticate(request)
        if inspect.isawaitable(result):
            result = anyio.from_thread.run(_await, result)
        return _check(result)
    except StarletteHTTPException:
        raise
    except ExternalAuthMisconfigured as exc:
        logger.error("celery-manager: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Sign-in through the host app is misconfigured; the server log says why. Nothing was changed.",
        ) from exc
    except Exception as exc:
        logger.exception("celery-manager: authenticate_request raised")
        raise HTTPException(
            status_code=500,
            detail="Sign-in through the host app failed on the server; the server log says why. Nothing was changed.",
        ) from exc
