"""Admin management of scoped API tokens for scripts."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from celery_manager.api.deps import Principal, require_admin
from celery_manager.api.serialization import utc
from celery_manager.auth.api_tokens import create_token, revoke_token
from celery_manager.control.audit import record as record_audit
from celery_manager.db.api_tokens import ApiToken

router = APIRouter(prefix="/api/tokens")


def _human(principal):
    if principal.source == "token":
        raise HTTPException(403, "API tokens cannot manage API tokens")


def _row(t: ApiToken) -> dict:
    return {"id": t.id, "name": t.name, "role": t.role, "owner": t.owner, "created_at": utc(t.created_at),
            "expires_at": utc(t.expires_at), "last_used_at": utc(t.last_used_at), "revoked_at": utc(t.revoked_at)}


@router.get("")
def list_tokens(request: Request, principal: Principal, username: str = Depends(require_admin)):
    _human(principal)
    with request.app.state.cm_session_factory() as session:
        return {"items": [_row(t) for t in session.query(ApiToken).order_by(ApiToken.id).all()]}


class CreateToken(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=16)
    expires_at: datetime | None = None


@router.post("")
def create(payload: CreateToken, request: Request, principal: Principal, username: str = Depends(require_admin)):
    """Create a token. The token is in this response only; store it now."""
    _human(principal)
    expires = payload.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    with request.app.state.cm_session_factory() as session:
        if session.query(ApiToken.id).filter_by(name=payload.name).first() is not None:
            raise HTTPException(409, "A token with this name already exists")
        try:
            row, token = create_token(session, payload.name, payload.role, str(username), expires)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        record_audit(session, username, "api_token_create", "api_token", payload.name,
                     {"role": payload.role, "expires_at": utc(expires)})
        return {**_row(row), "token": token}


@router.delete("/{token_id}")
def revoke(token_id: int, request: Request, principal: Principal, username: str = Depends(require_admin)):
    _human(principal)
    with request.app.state.cm_session_factory() as session:
        row = revoke_token(session, token_id)
        if row is None:
            raise HTTPException(404, "Unknown token")
        record_audit(session, username, "api_token_revoke", "api_token", row.name)
        return _row(row)
