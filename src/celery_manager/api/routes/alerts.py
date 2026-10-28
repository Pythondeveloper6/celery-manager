from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from celery_manager.alerts import DeliveryError, send_test
from celery_manager.api.deps import get_current_user, require_admin
from celery_manager.control import alerts as control

router = APIRouter(prefix="/api/alerts")


def _run(request: Request, fn, *args, **kwargs):
    """Call a control function and translate its refusals: 422 for bad
    input, 404 for an unknown id, 409 for a duplicate name. Each carries a
    sentence the dashboard shows as is."""
    with request.app.state.cm_session_factory() as session:
        try:
            return fn(session, *args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except control.AlertConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))


class NewRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    target: str = "*"
    params: dict[str, Any] = {}
    channel_ids: list[int] = []
    quiet_period_minutes: int = 60
    notify_resolved: bool = True
    enabled: bool = True


class RuleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    kind: str | None = None
    target: str | None = None
    params: dict[str, Any] | None = None
    channel_ids: list[int] | None = None
    quiet_period_minutes: int | None = None
    notify_resolved: bool | None = None
    enabled: bool | None = None


class NewChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    config: dict[str, Any] = {}


class ChannelChange(BaseModel):
    """`config` keys left out keep their stored value; that is how the
    dashboard edits a channel whose URL and secret it is never shown."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    config: dict[str, Any] | None = None


# Reads are open to every signed-in account (viewers can see what is watched
# and what is firing); channel URLs and secrets are masked in every response.


@router.get("/rules")
def read_rules(request: Request, _user: str = Depends(get_current_user)):
    return _run(request, lambda session: {"rules": control.list_rules(session)})


@router.post("/rules", status_code=201)
def create_rule(payload: NewRule, request: Request, username: str = Depends(require_admin)):
    return _run(request, control.create_rule, username, payload.model_dump())


@router.patch("/rules/{rule_id}")
def update_rule(rule_id: int, payload: RuleChange, request: Request, username: str = Depends(require_admin)):
    return _run(request, control.update_rule, username, rule_id, payload.model_dump(exclude_unset=True))


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, request: Request, username: str = Depends(require_admin)):
    _run(request, control.delete_rule, username, rule_id)
    return {"ok": True}


@router.get("/channels")
def read_channels(request: Request, _user: str = Depends(get_current_user)):
    return _run(request, lambda session: {"channels": control.list_channels(session)})


@router.post("/channels", status_code=201)
def create_channel(payload: NewChannel, request: Request, username: str = Depends(require_admin)):
    return _run(request, control.create_channel, username, payload.name, payload.kind, payload.config)


@router.patch("/channels/{channel_id}")
def update_channel(channel_id: int, payload: ChannelChange, request: Request, username: str = Depends(require_admin)):
    return _run(request, control.update_channel, username, channel_id, payload.model_dump(exclude_unset=True))


@router.delete("/channels/{channel_id}")
def delete_channel(channel_id: int, request: Request, username: str = Depends(require_admin)):
    _run(request, control.delete_channel, username, channel_id)
    return {"ok": True}


@router.post("/channels/{channel_id}/test")
def test_channel(channel_id: int, request: Request, username: str = Depends(require_admin)):
    """Send a test message now. 502 with the channel's answer if it fails.
    Runs in this API process, so email needs the SMTP options on *its*
    CeleryManagerConfig."""
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        try:
            send_test(session, username, channel_id, config)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except DeliveryError as exc:
            raise HTTPException(status_code=502, detail=f"The test message was not delivered: {exc}")
    return {"ok": True}


@router.get("/incidents")
def read_incidents(
    request: Request,
    state: str | None = Query(default=None, pattern="^(firing|resolved)$"),
    rule_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    _user: str = Depends(get_current_user),
):
    """Newest first. Filter with `state` (firing | resolved) and `rule_id`."""
    return _run(request, lambda session: {"incidents": control.list_incidents(session, state, rule_id, limit)})
