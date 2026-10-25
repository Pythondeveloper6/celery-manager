from __future__ import annotations

from fastapi import Cookie, HTTPException, Request

from celery_manager.auth.sessions import verify_session
from celery_manager.auth.users import user_exists

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


def get_current_user(request: Request, session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> str:
    config = request.app.state.cm_config
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    username = verify_session(config.secret_key, session)
    if username is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    # Sessions are stateless signed tokens, so a valid signature alone would
    # keep a deleted account working for the rest of its 7 days.
    with request.app.state.cm_session_factory() as db_session:
        if not user_exists(db_session, username):
            raise HTTPException(status_code=401, detail="invalid or expired session")
    return username
