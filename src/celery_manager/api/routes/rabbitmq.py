from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from celery_manager.api.deps import get_current_user, require_admin
from celery_manager.control import rabbitmq
from celery_manager.control.rabbitmq import (
    RabbitMQError,
    RabbitMQNotConfigured,
    RabbitMQNotFound,
    browse_dlq,
    list_dlqs,
    overview,
    requeue_dlq,
)

router = APIRouter(prefix="/api/rabbitmq")

_MAX_QUEUE_NAME = 255
_MAX_KEY = 255


def _check_name(name: str) -> None:
    if not name or len(name) > _MAX_QUEUE_NAME:
        raise HTTPException(status_code=400, detail="invalid queue name")


def _answer(fn, *args, **kwargs):
    """Map the control layer's errors to HTTP. "Not configured" is a normal
    answer (200, `configured: false`) on the read routes, which call
    `_not_configured` themselves; it only gets here from requeue."""
    try:
        return fn(*args, **kwargs)
    except RabbitMQNotConfigured as exc:
        raise HTTPException(status_code=409, detail=f"RabbitMQ isn't set up here: {exc}") from None
    except RabbitMQNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except RabbitMQError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.get("/overview")
def get_overview(request: Request, username: str = Depends(get_current_user)):
    """Version, nodes (memory, disk, file descriptors, sockets) and alarms.

    `{"configured": false, "reason": ...}` when the management API isn't
    configured or the broker isn't RabbitMQ; 502 when it can't be asked.
    """
    return _answer(overview, request.app.state.cm_config)


@router.get("/dlqs")
def get_dlqs(request: Request, username: str = Depends(get_current_user)):
    """Detected dead-letter queues, and warnings about dead letters that
    would be dropped (a dead-letter exchange with nothing bound)."""
    return _answer(list_dlqs, request.app.state.cm_config)


@router.get("/dlqs/{name}/messages")
def get_dlq_messages(name: str, request: Request, count: int = rabbitmq.BROWSE_MAX, reveal_args: bool = False,
                     username: str = Depends(get_current_user)):
    """Up to 20 messages from the head of a dead-letter queue, approximate
    (reading puts them back, marked redelivered). Arguments are masked
    unless `reveal_args=true`."""
    _check_name(name)
    return _answer(browse_dlq, request.app.state.cm_config, name, count=count, reveal_args=reveal_args)


class RequeueRequest(BaseModel):
    # Message keys from the browse answer (`key`); leave out for "all".
    keys: list[str] | None = Field(default=None, max_length=rabbitmq.REQUEUE_MAX)
    limit: int = Field(default=rabbitmq.REQUEUE_DEFAULT, ge=1, le=rabbitmq.REQUEUE_MAX)
    # Send messages past the loop-protection limit too.
    force: bool = False


@router.post("/dlqs/{name}/requeue")
def requeue(name: str, body: RequeueRequest, request: Request, username: str = Depends(require_admin)):
    """Move dead-lettered messages back to the exchange and routing key they
    were first published to. Admin; audited. Moves nothing it can't confirm.

    Answers 200 with `moved` and, if it stopped early, `error`; 404 if
    `name` isn't a dead-letter queue, 409 if RabbitMQ isn't set up, 502 if
    the management API or the broker can't be reached before anything moved.
    """
    _check_name(name)
    if body.keys is not None and (not body.keys or any(not k or len(k) > _MAX_KEY for k in body.keys)):
        raise HTTPException(status_code=422, detail="keys must be a non-empty list of message keys")
    config = request.app.state.cm_config
    with request.app.state.cm_session_factory() as session:
        return _answer(requeue_dlq, config, session, name, username,
                       keys=body.keys, limit=body.limit, force=body.force)
