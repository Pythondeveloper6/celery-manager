from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from celery_manager.api.deps import get_current_user
from celery_manager.control.broker import broker_health

router = APIRouter(prefix="/api/broker")


@router.get("/health")
def get_broker_health(request: Request, username: str = Depends(get_current_user)):
    """Is the broker reachable, how fast, and (Redis) how full.

    Always 200, even when the broker is down: that is the answer
    (`reachable: false` with `error`), and the dashboard polls this next to
    the queue list. The broker URL comes back with its password masked.
    """
    config = request.app.state.cm_config
    health = broker_health(config.celery_app)
    details = health.get("details")
    # The alarms live in the RabbitMQ panel when the management API is set up
    # (control/rabbitmq.py); say so rather than "not configured".
    if isinstance(details, dict) and "management" in details and config.rabbitmq_management_url:
        details["management"] = "configured"
    return health
