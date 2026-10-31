from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from celery_manager.api.deps import get_current_user, require_admin
from celery_manager.control.depth_history import MAX_HISTORY_HOURS, history
from celery_manager.control.latency import wait_stats
from celery_manager.control.messages import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MessageActionUnsupported,
    MessageNotFound,
    MessageTaken,
    UnknownQueue,
    delete_message,
    move_message,
    peek_messages,
)
from celery_manager.control.queues import (
    QueueBrokerError,
    QueueNotFound,
    list_queues,
    preview_purge,
    purge_queue,
)
from celery_manager.control.rabbitmq import annotate_queues

router = APIRouter(prefix="/api/queues")

# Queue names are free text to the broker, but anything longer than the
# `task_runs.queue` column could never match a recorded run, and an empty name
# would make AMQP declare a server-named queue.
_MAX_QUEUE_NAME = 255


@router.get("")
def get_queues(request: Request, workers: bool = True, username: str = Depends(get_current_user)):
    """Known queues with broker depth and recorded-queued counts.

    Always 200, even when the broker is down: broker trouble is reported in
    `broker.error` with `depth: null` per queue, because the recorded counts
    are still useful and the dashboard polls this every few seconds.
    `?workers=false` skips the (cached) worker active_queues inspect.

    With RabbitMQ management configured, each queue also carries a
    `rabbitmq` object (consumers, unacknowledged, rates, type, arguments)
    and `rabbitmq` at the top level says whether that worked; see
    control/rabbitmq.py.
    """
    config = request.app.state.cm_config
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        listing = list_queues(config.celery_app, session, include_workers=workers)
        # Queue wait over the last 15 minutes (the same window as the
        # metrics), `null` for a queue with no timed run in it.
        stats = wait_stats(session)
        for queue in listing["queues"]:
            queue["wait"] = stats["by_queue"].get(queue["name"])
        listing["wait_window_seconds"] = stats["window_seconds"]
    return annotate_queues(config, listing)


@router.get("/{name}/history")
def get_queue_history(
    name: str,
    request: Request,
    hours: Annotated[float, Query(gt=0, le=MAX_HISTORY_HOURS)] = 24,
    username: str = Depends(get_current_user),
):
    """Sampled broker depth of one queue over the last `hours`, downsampled
    to at most a few hundred points (each the highest depth in its bucket).
    Empty `points` for a queue that was never sampled: unknown names are not
    an error, because a queue can be sampled before anyone has seen it here."""
    if not name or len(name) > _MAX_QUEUE_NAME:
        raise HTTPException(status_code=400, detail="invalid queue name")
    with request.app.state.cm_session_factory() as session:
        return history(session, name, hours)


class PurgeRequest(BaseModel):
    dry_run: bool = False
    # The queue name typed by the operator. When sent it must match exactly;
    # with `require_typed_confirmation` on it must always be sent.
    confirm: str | None = Field(default=None, max_length=_MAX_QUEUE_NAME)


@router.post("/{name}/purge")
def purge(name: str, request: Request, payload: PurgeRequest | None = None, username: str = Depends(require_admin)):
    """Remove every ready message from one queue. Destructive; audited.

    `{"dry_run": true}` only reports the current depth and how many recorded
    runs would be marked revoked; nothing is purged or audited. A `confirm`
    value must equal the queue name (400 otherwise), and is mandatory when
    `require_typed_confirmation` is configured.

    404 if the broker says the queue doesn't exist (RabbitMQ), 503 if the
    broker can't be reached, timed out, or can't be purged from here (the
    in-memory transport). Nothing is marked or audited on failure.
    """
    if not name or len(name) > _MAX_QUEUE_NAME:
        raise HTTPException(status_code=400, detail="invalid queue name")
    payload = payload or PurgeRequest()
    config = request.app.state.cm_config
    celery_app = config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        try:
            if payload.dry_run:
                return preview_purge(celery_app, session, name)
            if payload.confirm is not None and payload.confirm != name:
                raise HTTPException(status_code=400, detail="confirmation does not match the queue name")
            if payload.confirm is None and config.require_typed_confirmation:
                raise HTTPException(status_code=400, detail="type the queue name to confirm this purge")
            return purge_queue(celery_app, session, name, username=username)
        except QueueNotFound:
            raise HTTPException(status_code=404, detail=f"the broker has no queue named {name!r}")
        except QueueBrokerError as exc:
            raise HTTPException(status_code=503, detail=str(exc))


def _check_name(name: str) -> None:
    if not name or len(name) > _MAX_QUEUE_NAME:
        raise HTTPException(status_code=400, detail="invalid queue name")


@router.get("/{name}/messages")
def get_messages(
    name: str,
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    username: str = Depends(get_current_user),
):
    """Messages waiting in one queue, next to run first, without taking them.

    Exact and read-only on Redis. On RabbitMQ only with `queue_browser_amqp`
    on, and then `approximate: true` with the `side_effects` listed; otherwise
    (and on other transports) `supported: false` and a `reason`. 503 if the
    broker can't be reached.
    """
    _check_name(name)
    config = request.app.state.cm_config
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        try:
            return peek_messages(
                config.celery_app,
                session,
                name,
                limit=limit,
                offset=offset,
                show_args=config.queue_browser_show_args,
                allow_amqp=config.queue_browser_amqp,
            )
        except QueueBrokerError as exc:
            raise HTTPException(status_code=503, detail=str(exc))


@contextmanager
def _message_errors(name: str, task_id: str):
    """The HTTP answer for each way taking one message can fail."""
    try:
        yield
    except MessageNotFound:
        raise HTTPException(
            status_code=404,
            detail=f"no message for task {task_id} is waiting in {name}; a worker may already have taken it",
        )
    except MessageTaken:
        raise HTTPException(status_code=409, detail="already taken by a worker; nothing was changed")
    except MessageActionUnsupported as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except QueueBrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.delete("/{name}/messages/{task_id}")
def delete_one(name: str, task_id: str, request: Request, username: str = Depends(require_admin)):
    """Remove one waiting message for good (Redis only). Destructive; audited.

    404 if it isn't waiting there, 409 if a worker took it first or the broker
    isn't Redis, 503 if the broker can't be reached.
    """
    _check_name(name)
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session, _message_errors(name, task_id):
        return delete_message(celery_app, session, name, task_id, username=username)


class MoveRequest(BaseModel):
    target: str = Field(min_length=1, max_length=_MAX_QUEUE_NAME)


@router.post("/{name}/messages/{task_id}/move")
def move_one(name: str, task_id: str, body: MoveRequest, request: Request, username: str = Depends(require_admin)):
    """Move one waiting message to another known queue (Redis only). Audited.

    400 for an unknown target queue (one that is neither configured nor seen
    on a run or a worker) or the same queue; otherwise as delete.
    """
    _check_name(name)
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        try:
            with _message_errors(name, task_id):
                return move_message(celery_app, session, name, task_id, body.target, username=username)
        except UnknownQueue:
            raise HTTPException(
                status_code=400,
                detail=f"{body.target!r} isn't a known queue: it must be configured, routed to, or seen on a run",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
