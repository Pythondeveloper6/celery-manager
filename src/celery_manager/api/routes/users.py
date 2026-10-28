from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from celery_manager.api.deps import (
    SESSION_COOKIE,
    Principal,
    cookie_path,
    require_admin,
    require_role,
)
from celery_manager.auth.external import SOURCE_LOCAL
from celery_manager.auth.sessions import sign_session
from celery_manager.auth.users import session_fingerprint
from celery_manager.control import users as control

router = APIRouter(prefix="/api")


def _run(request: Request, fn, *args, **kwargs):
    """Call a control function and translate its refusals. 422 for bad
    input, 404 for an unknown account, 409 for a request the safety rules or
    a duplicate refuse. All carry a sentence written for the person who
    clicked, which the dashboard shows as is."""
    with request.app.state.cm_session_factory() as session:
        try:
            return fn(session, *args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except control.UserConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))


class NewUser(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
    role: str = "viewer"


class RoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str


class PasswordReset(BaseModel):
    """Either ``password`` or ``generate: true``."""

    model_config = ConfigDict(extra="forbid")

    password: str | None = None
    generate: bool = False


class OwnPasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str


@router.get("/users")
def read_users(request: Request, _admin: str = Depends(require_admin)):
    """Every dashboard account. Admin only, though it is a GET: it lists who
    can sign in. Each entry has ``source``: ``"local"`` for accounts stored
    by celery-manager itself."""
    return _run(request, lambda session: {"users": control.list_users(session)})


@router.post("/users", status_code=201)
def create_dashboard_user(payload: NewUser, request: Request, username: str = Depends(require_admin)):
    return _run(request, control.add_user, username, payload.username, payload.password, payload.role)


@router.patch("/users/{target}")
def update_dashboard_user(target: str, payload: RoleChange, request: Request, username: str = Depends(require_admin)):
    return _run(request, control.change_role, username, target, payload.role)


@router.post("/users/{target}/password")
def reset_dashboard_user_password(
    target: str,
    payload: PasswordReset,
    request: Request,
    response: Response,
    username: str = Depends(require_admin),
):
    """Admin sets a new password (or asks for a generated one, returned once
    in ``generated_password``). Ends every session the account has."""
    generated = _run(
        request, control.reset_password, username, target, payload.password, generate=payload.generate
    )
    response.headers["Cache-Control"] = "no-store"
    return {"username": target, "generated_password": generated}


@router.delete("/users/{target}")
def delete_dashboard_user(target: str, request: Request, username: str = Depends(require_admin)):
    _run(request, control.delete_user, username, target)
    return {"ok": True}


@router.post("/me/password")
def change_my_password(
    payload: OwnPasswordChange,
    request: Request,
    response: Response,
    principal: Principal,
    # The lowest role: every signed-in account may change its own password.
    username: str = Depends(require_role("viewer")),
):
    """Change the signed-in account's own password. Other sessions of the
    account end; this one stays signed in through a freshly issued cookie."""
    config = request.app.state.cm_config
    if principal.source != SOURCE_LOCAL:
        # Someone signed in through the host app has no password here. A
        # local account that happens to share their name is someone else's
        # credential, not theirs to change.
        raise HTTPException(
            status_code=409,
            detail=f"Your password is managed by {config.login_provider_name}; change it there.",
        )
    with request.app.state.cm_session_factory() as session:
        try:
            user = control.change_own_password(session, username, payload.current_password, payload.new_password)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Not allowed: {exc}.")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
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
    )
    return {"ok": True}
