from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from celery_manager.api.deps import get_current_user
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
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return {"username": user.username}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@router.get("/whoami")
def whoami(username: str = Depends(get_current_user)):
    return {"username": username}
