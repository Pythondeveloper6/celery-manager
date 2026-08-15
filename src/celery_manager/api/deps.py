from __future__ import annotations

from fastapi import Cookie, HTTPException, Request

from celery_manager.auth.sessions import verify_session


def get_current_user(request: Request, session: str | None = Cookie(default=None)) -> str:
    config = request.app.state.cm_config
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    username = verify_session(config.secret_key, session)
    if username is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return username
