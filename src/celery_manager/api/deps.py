from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request

from celery_manager.auth.roles import role_allows, validate_role
from celery_manager.auth.sessions import verify_session_claims
from celery_manager.auth.users import get_user_auth, session_fingerprint

# Not plain `session`: celery-manager is usually mounted inside a host app,
# and Flask's and Starlette's own session cookies are both called `session`
# — two cookies of the same name on overlapping paths clobber each other.
SESSION_COOKIE = "cm_session"


def cookie_path(request: Request) -> str:
    """Scope the session cookie to wherever this app is mounted, so a
    dashboard under `/celery` doesn't send its credential to every other
    path on the host. Must be identical on set and delete — a browser only
    removes a cookie whose path matches the one it was set with."""
    return request.scope.get("root_path") or "/"


@dataclass(frozen=True)
class CurrentUser:
    username: str
    role: str


def get_current_principal(
    request: Request, session: str | None = Cookie(default=None, alias=SESSION_COOKIE)
) -> CurrentUser:
    """The signed-in account and its role, or a 401."""
    config = request.app.state.cm_config
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    claims = verify_session_claims(config.secret_key, session)
    if claims is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    username, fingerprint = claims
    # Sessions are stateless signed tokens, so a valid signature alone would
    # keep a deleted account working for the rest of its 7 days. The role is
    # read here on every request for the same reason: a demotion takes
    # effect on the very next request, not when the cookie expires. The
    # password hash is read for the same reason: the cookie carries a
    # fingerprint of it, so a password reset ends every older session.
    with request.app.state.cm_session_factory() as db_session:
        auth = get_user_auth(db_session, username)
    if auth is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    role, password_hash = auth
    if not hmac.compare_digest(fingerprint, session_fingerprint(config.secret_key, password_hash)):
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return CurrentUser(username=username, role=role)


# The signed-in account as a dependency-injected parameter type.
Principal = Annotated[CurrentUser, Depends(get_current_principal)]


def get_current_user(principal: Principal) -> str:
    """Any signed-in account, whatever its role. For read-only routes."""
    return principal.username


# Attribute set on every dependency `require_role` builds, so the app can
# check at startup that each state-changing route declares a role.
REQUIRED_ROLE_ATTR = "__cm_required_role__"


def require_role(role: str):
    """A dependency that admits only accounts with at least ``role`` and
    yields the username (a drop-in for ``get_current_user``), so routes keep
    attributing audit entries to the acting user.

    Anyone signed in but below ``role`` gets a 403; not signed in is still a
    401, from ``get_current_principal``."""
    validate_role(role)

    def dependency(principal: Principal) -> str:
        if not role_allows(principal.role, role):
            raise HTTPException(
                status_code=403,
                # Shown as-is by the dashboard, so written for a person.
                detail=f"Not allowed: this action needs the {role} role (you are {principal.role}).",
            )
        return principal.username

    setattr(dependency, REQUIRED_ROLE_ATTR, role)
    dependency.__name__ = f"require_{role}"
    return dependency


require_operator = require_role("operator")
require_admin = require_role("admin")
