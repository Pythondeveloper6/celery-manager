from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from celery_manager.api.deps import SESSION_COOKIE, Principal, cookie_path, require_role
from celery_manager.auth.external import SOURCE_LOCAL
from celery_manager.auth.sessions import sign_session
from celery_manager.auth.users import session_fingerprint, verify_password

require_viewer = require_role("viewer")

router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/auth/options")
def auth_options(request: Request):
    """How to sign in, for the login screen, so unauthenticated like
    `GET /api/settings`. Nothing here is a secret: the login and logout URLs
    are links the host app shows to everyone anyway.

    ``local_login``: whether the username/password form works.
    ``external``: whether the host app can sign people in
    (``authenticate_request`` is set); the dashboard then labels its own
    accounts "local". ``login_url`` may contain ``{next}`` for the dashboard
    to fill in with its own address."""
    config = request.app.state.cm_config
    return {
        "local_login": config.local_login_enabled,
        "external": config.authenticate_request is not None or bool(config.principal_providers),
        "live_stream": config.live_stream,
        "allow_queue_override": config.allow_queue_override,
        "retry_edit_args": config.retry_edit_args,
        "provider_name": config.login_provider_name,
        "login_url": config.login_url,
        "logout_url": config.logout_url,
    }


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response):
    config = request.app.state.cm_config
    if not config.local_login_enabled:
        # Refused before the password is even looked at: a local account
        # that still exists must not become a way in once the host app is
        # meant to be the only one.
        raise HTTPException(
            status_code=403,
            detail=f"Sign-in with a dashboard password is turned off. Sign in with {config.login_provider_name}.",
        )
    session_factory = request.app.state.cm_session_factory
    with session_factory() as db_session:
        user = verify_password(db_session, payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = sign_session(
        config.secret_key, user.username, session_fingerprint(config.secret_key, user.password_hash)
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        path=cookie_path(request),
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
        max_age=config.session_max_age_seconds,
    )
    return {"username": user.username, "role": user.role, "source": SOURCE_LOCAL}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    config = request.app.state.cm_config
    # Same attributes as the login cookie: some browsers ignore a deletion
    # whose Secure/SameSite don't match, and a mismatched path deletes nothing.
    response.delete_cookie(
        SESSION_COOKIE,
        path=cookie_path(request),
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
    )
    return {"ok": True}


@router.get("/whoami")
def whoami(principal: Principal):
    """The signed-in account and its role. The dashboard uses the role only
    to hide controls the account can't use; the server checks it again on
    every state-changing request. ``source`` is ``"local"`` for
    celery-manager's own accounts and ``"external"`` for someone the host
    app signed in, whose password and sign-out belong to the host."""
    return {"username": principal.username, "role": principal.role, "source": principal.source, **({"subject": principal.subject} if principal.subject else {})}


@router.post("/auth/bridge")
def bridge(request: Request, response: Response, principal: Principal, username: str = Depends(require_viewer)):
    """Opt-in, short-lived dashboard session. Role changes apply on expiry."""
    config = request.app.state.cm_config
    if not config.session_bridge_enabled:
        raise HTTPException(404, "Session bridge is disabled")
    if principal.source == SOURCE_LOCAL:
        raise HTTPException(403, "The bridge requires external authentication")
    # Do not let a bridge cookie renew itself indefinitely.
    from celery_manager.auth.external import authenticate_external
    external = authenticate_external(config, request)
    if external is None or external.role is None:
        raise HTTPException(401, "Fresh external authentication is required")
    from celery_manager.auth.sessions import sign_bridge
    response.set_cookie(SESSION_COOKIE, sign_bridge(config.secret_key, external.username, external.role, external.subject),
                        max_age=config.bridge_max_age_seconds, path=cookie_path(request),
                        httponly=True, secure=config.cookie_secure, samesite="lax")
    return {"username": external.username, "role": external.role, "expires_in": config.bridge_max_age_seconds}
