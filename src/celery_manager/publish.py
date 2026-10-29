"""Optional producer hook: make host-dispatched tasks visible immediately."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from celery import current_app, current_task, signals

from celery_manager.db.runs import lock_task_run

logger = logging.getLogger(__name__)


def install_publish_hook(celery_app, *, session_factory=None, initiator=None):
    """Record protocol-v2 messages after successful publish. Returns uninstall().

    A before-publish handler adds initiator headers; seeding happens after
    publish so a broker failure does not leave a phantom queued task. Storage
    and initiator failures never change the producer's outcome.
    """
    if session_factory is None:
        from celery_manager import runtime
        session_factory = runtime.session_factory_for(celery_app)
        if session_factory is None:
            raise RuntimeError("Call celery_manager.setup(config) before install_publish_hook")

    def belongs():
        return current_app._get_current_object() is celery_app

    def before(sender=None, headers=None, **unused):
        if not belongs() or headers is None:
            return
        def valid(value):
            return isinstance(value, str) and 0 < len(value) <= 255 and value.isprintable()
        # Explicit dispatch identity wins. A child published inside a worker
        # inherits that worker request's actor, never another Celery app's.
        if valid(headers.get("cm_initiator")):
            return
        try:
            task = current_task._get_current_object()
            parent = (getattr(task.request, "headers", None) or {}) if task is not None and task.app is celery_app else {}
            value = parent.get("cm_initiator")
            if not valid(value) and initiator is not None:
                value = initiator()
            if valid(value):
                headers["cm_initiator"] = value
        except Exception:
            logger.exception("celery-manager: initiator callback failed")

    def after(sender=None, headers=None, body=None, routing_key=None, **unused):
        if not belongs() or not headers or not headers.get("id"):
            return
        try:
            args, kwargs = body[:2]
            eta = headers.get("eta")
            eta = datetime.fromisoformat(eta) if isinstance(eta, str) else None
            with session_factory() as session:
                lock_task_run(session, headers["id"], seed={
                    "task_name": sender or headers.get("task") or "unknown", "status": "queued",
                    "args": json.dumps(args, default=repr), "kwargs": json.dumps(kwargs, default=repr),
                    "queue": routing_key, "eta": eta, "initiator": headers.get("cm_initiator"),
                    "sent_at": datetime.now(timezone.utc),
                })
                session.commit()
        except Exception:
            logger.exception("celery-manager: published task could not be recorded")

    # One hook per app. Reinstallation replaces it rather than doubling writes.
    old = getattr(celery_app, "_cm_uninstall_publish_hook", None)
    if old:
        old()
    signals.before_task_publish.connect(before, weak=False)
    signals.after_task_publish.connect(after, weak=False)

    def uninstall():
        signals.before_task_publish.disconnect(before)
        signals.after_task_publish.disconnect(after)
        if getattr(celery_app, "_cm_uninstall_publish_hook", None) is uninstall:
            del celery_app._cm_uninstall_publish_hook

    celery_app._cm_uninstall_publish_hook = uninstall
    return uninstall
