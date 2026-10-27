from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from celery_manager.api.deps import get_current_user, require_admin
from celery_manager.control.queues import (
    QueueBrokerError,
    QueueNotFound,
    list_queues,
    purge_queue,
)

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
    """
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        return list_queues(celery_app, session, include_workers=workers)


@router.post("/{name}/purge")
def purge(name: str, request: Request, username: str = Depends(require_admin)):
    """Remove every ready message from one queue. Destructive; audited.

    404 if the broker says the queue doesn't exist (RabbitMQ), 503 if the
    broker can't be reached, timed out, or can't be purged from here (the
    in-memory transport). Nothing is marked or audited on failure.
    """
    if not name or len(name) > _MAX_QUEUE_NAME:
        raise HTTPException(status_code=400, detail="invalid queue name")
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        try:
            return purge_queue(celery_app, session, name, username=username)
        except QueueNotFound:
            raise HTTPException(status_code=404, detail=f"the broker has no queue named {name!r}")
        except QueueBrokerError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
