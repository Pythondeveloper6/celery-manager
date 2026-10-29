"""Authenticated lease inspection and ownership-conditional administrative release."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from celery_manager.api.deps import get_current_user, require_admin
from celery_manager.control.audit import record
from celery_manager.locks import provider_for

router = APIRouter(prefix="/api/locks")


class ReleaseLease(BaseModel):
    key: str = Field(min_length=1, max_length=512)
    lease_id: str = Field(min_length=1, max_length=128)


@router.get("")
def list_locks(request: Request, limit: int = Query(100, ge=1, le=1000),
               username: str = Depends(get_current_user)):
    try:
        rows = provider_for(request.app.state.cm_config).list(limit)
    except Exception as exc:
        raise HTTPException(503, "Lock authority unavailable") from exc
    return {"locks": [asdict(row) for row in rows], "snapshot": True}


@router.post("/release")
def release_lock(body: ReleaseLease, request: Request, username: str = Depends(require_admin)):
    try:
        provider = provider_for(request.app.state.cm_config)
        lease = provider.inspect(body.key)
    except Exception as exc:
        raise HTTPException(503, "Lock authority unavailable") from exc
    with request.app.state.cm_session_factory() as session:
        # Record intent before touching Redis; an unavailable audit database
        # must not permit an unaudited administrative release.
        record(session, username, "singleton_release_requested", "singleton_lock", body.key,
               {"lease_id": body.lease_id, "owner_task_id": lease.task_id if lease else None})
        if lease is None or lease.lease_id != body.lease_id:
            raise HTTPException(409, "Lease expired or ownership changed; inspect locks again")
        try:
            released = provider.release(lease)
        except Exception as exc:
            raise HTTPException(503, "Lock authority unavailable; release outcome uncertain") from exc
        record(session, username, "singleton_release" if released else "singleton_release_refused",
               "singleton_lock", body.key, {"lease_id": body.lease_id, "owner_task_id": lease.task_id})
        if not released:
            raise HTTPException(409, "Lease expired or ownership changed; inspect locks again")
    return {"released": True}
