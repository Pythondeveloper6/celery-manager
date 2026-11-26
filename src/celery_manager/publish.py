"""Optional producer hook: make host-dispatched tasks visible immediately."""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone

from celery import current_app, current_task, signals

from celery_manager.db.runs import lock_task_run
from celery_manager.redaction import redact_run
from celery_manager.telemetry import Skipped, guarded

logger = logging.getLogger(__name__)

PUBLISH_QUEUE_SIZE = 10_000


def install_publish_hook(celery_app, *, session_factory=None, initiator=None, context=None):
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

    hook_config = getattr(session_factory, "kw", {}).get("info", {}).get("cm_config")

    def context_before(sender=None, headers=None, **unused):
        if not belongs() or headers is None:
            return
        try:
            from celery_manager.observation import _context
            from celery_manager.run_context import normalize_context
            task = current_task._get_current_object()
            parent = (getattr(task.request, "headers", None) or {}) if task is not None and task.app is celery_app else {}
            inherited = _context.get()
            value = context() if context is not None else inherited or parent.get("cm_context") or headers.get("cm_context")
            if value is not None:
                headers["cm_context"] = normalize_context(value, hook_config)
        except Exception:  # noqa: BLE001 - telemetry must preserve producer outcome
            headers.pop("cm_context", None)
            logger.warning("celery-manager: invalid host context was withheld")

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

    def record(sender, headers, body, routing_key):
        try:
            args, kwargs = body[:2]
            eta = headers.get("eta")
            eta = datetime.fromisoformat(eta) if isinstance(eta, str) else None
            with guarded(hook_config), session_factory() as session:
                row = lock_task_run(session, headers["id"], seed={
                    "task_name": sender or headers.get("task") or "unknown", "status": "queued",
                    "args": json.dumps(args, default=repr), "kwargs": json.dumps(kwargs, default=repr),
                    "queue": routing_key, "eta": eta, "initiator": headers.get("cm_initiator"),
                    "sent_at": datetime.now(timezone.utc),
                })
                redact_run(hook_config, row, celery_app)
                if headers.get("cm_context") is not None:
                    from celery_manager.run_context import (
                        merge_storage_context,
                        normalize_context,
                    )
                    merge_storage_context(row, normalize_context(headers["cm_context"], hook_config))
                session.commit()
        except Skipped:
            return
        except Exception:
            logger.exception("celery-manager: published task could not be recorded")

    threaded = getattr(hook_config, "publish_hook_mode", "sync") == "thread"
    drainer = {"pid": None, "queue": None}
    drainer_lock = threading.Lock()

    def pending_queue():
        # Started on first use in each process: a thread started before a
        # pre-forking server forks does not exist in its workers.
        with drainer_lock:
            if drainer["pid"] != os.getpid():
                work = queue.Queue(maxsize=PUBLISH_QUEUE_SIZE)

                def drain():
                    while True:
                        record(*work.get())

                threading.Thread(target=drain, name="celery-manager-publish", daemon=True).start()
                drainer.update(pid=os.getpid(), queue=work)
            return drainer["queue"]

    def after(sender=None, headers=None, body=None, routing_key=None, **unused):
        if not belongs() or not headers or not headers.get("id"):
            return
        if not threaded:
            record(sender, headers, body, routing_key)
            return
        try:
            pending_queue().put_nowait((sender, dict(headers), body, routing_key))
        except queue.Full:
            # shortcut: a full queue drops the row (the events listener still
            # records the run); a bigger queue only delays the same loss.
            logger.warning("celery-manager: publish queue full; run %s not seeded", headers.get("id"))

    # One hook per app. Reinstallation replaces it rather than doubling writes.
    old = getattr(celery_app, "_cm_uninstall_publish_hook", None)
    if old:
        old()
    signals.before_task_publish.connect(context_before, weak=False)
    signals.before_task_publish.connect(before, weak=False)
    signals.after_task_publish.connect(after, weak=False)

    def uninstall():
        signals.before_task_publish.disconnect(context_before)
        signals.before_task_publish.disconnect(before)
        signals.after_task_publish.disconnect(after)
        if getattr(celery_app, "_cm_uninstall_publish_hook", None) is uninstall:
            del celery_app._cm_uninstall_publish_hook

    celery_app._cm_uninstall_publish_hook = uninstall
    return uninstall
