from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from celery_manager.api.deps import SESSION_COOKIE, cookie_path, get_current_user
from celery_manager.auth.sessions import sign_session
from celery_manager.auth.users import verify_password

router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response):
    config = request.app.state.cm_config
    session_factory = request.app.state.cm_session_factory
    with session_factory() as db_session:
        user = verify_password(db_session, payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = sign_session(config.secret_key, user.username)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        path=cookie_path(request),
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
    )
    return {"username": user.username}


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
def whoami(username: str = Depends(get_current_user)):
    return {"username": username}
