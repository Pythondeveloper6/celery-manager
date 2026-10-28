"""Alerts: tell Slack, a webhook or a mailbox when a task's failure rate
jumps, a queue backs up, a worker goes offline or a periodic task misses
its run. Rules and channels are managed in the dashboard (or
`celery_manager.control.alerts`); this package judges them and sends the
messages. See docs/alerts.md.

Run it with `celery-manager alerts ...`, alongside the events listener with
`celery-manager events --with-alerts ...`, or from your own code with
`run_forever` / `start_in_background`.
"""

from celery_manager.alerts.evaluator import (
    WORKER_FORGET_AFTER,
    evaluate_once,
    run_forever,
    send_test,
    start_in_background,
)
from celery_manager.alerts.senders import AlertMessage, DeliveryError

__all__ = [
    "WORKER_FORGET_AFTER",
    "AlertMessage",
    "DeliveryError",
    "evaluate_once",
    "run_forever",
    "send_test",
    "start_in_background",
]
