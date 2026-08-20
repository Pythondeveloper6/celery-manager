from __future__ import annotations

from datetime import datetime, timezone

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import TaskRun

_STATUS_BY_EVENT = {
    "task-sent": "queued",
    "task-received": "queued",
    "task-started": "started",
    "task-succeeded": "succeeded",
    "task-failed": "failed",
    "task-retried": "retried",
    "task-revoked": "revoked",
}

# Statuses the decorator owns outright and that no Celery lifecycle event can
# describe, so the listener must never overwrite them.
#
# `skipped` means "@track matched a schedule override and never called the task
# body". Celery knows nothing about that: the wrapper still returns normally,
# so the worker emits a perfectly ordinary task-succeeded for the same uuid.
# In the documented two-process topology (worker + a separate
# `celery-manager events` process) that event lands after the decorator's
# write and used to flip the row back to `succeeded` — so a paused occurrence
# displayed as if it had run, exactly whenever the listener is running.
#
# Deliberately narrow: only `skipped`. `succeeded`/`failed`/`retried` are
# shared vocabulary where a later listener update is the normal, correct
# sequence, and blanket-guarding them would break the ordinary case.
_DECORATOR_OWNED_STATUSES = frozenset({"skipped"})


def upsert_task_run(session, event: dict) -> None:
    """Apply a single Celery event to the task_runs table.

    Pure function over a SQLAlchemy session so it's testable without a
    real broker/receiver — EventListener.run() below just feeds it events.
    """
    event_type = event["type"]
    status = _STATUS_BY_EVENT.get(event_type)
    if status is None:
        return  # worker-* events etc. are not task_runs concerns here

    task_id = event["uuid"]
    run = session.query(TaskRun).filter_by(task_id=task_id).one_or_none()
    if run is None:
        run = TaskRun(task_id=task_id, task_name=event.get("name", "unknown"), status=status)
        session.add(run)
    elif run.status not in _DECORATOR_OWNED_STATUSES:
        run.status = status
    # else: leave the decorator-owned terminal status alone (see
    # _DECORATOR_OWNED_STATUSES). Only `status` is protected — every other
    # field below still updates, so a skipped run keeps picking up the
    # worker/queue the listener knows about and the decorator doesn't.

    if "name" in event:
        run.task_name = event["name"]
    if "queue" in event:
        run.queue = event["queue"]
    if "hostname" in event:
        run.worker = event["hostname"]
    if "args" in event:
        run.args = str(event["args"])
    if "kwargs" in event:
        run.kwargs = str(event["kwargs"])
    if event_type == "task-started":
        run.started_at = datetime.now(timezone.utc)
    if event_type in ("task-succeeded", "task-failed"):
        run.finished_at = datetime.now(timezone.utc)
    if "runtime" in event:
        run.runtime_seconds = event["runtime"]
    if "exception" in event:
        run.exception = event["exception"]
    if "traceback" in event:
        run.traceback = event["traceback"]


class EventListener:
    """Long-running consumer of the Celery event bus. Run via the
    `celery-manager events` CLI command (Task 16 wires this up for the
    Django demo; see cli.py)."""

    def __init__(self, config: CeleryManagerConfig, session_factory, event_bus=None):
        self._config = config
        self._session_factory = session_factory
        self._event_bus = event_bus

    def run(self) -> None:
        app = self._config.celery_app
        state = app.events.State()

        def on_event(event):
            state.event(event)
            with self._session_factory() as session:
                upsert_task_run(session, event)
                session.commit()
            if self._event_bus is not None:
                self._event_bus.publish({"task_id": event.get("uuid"), "type": event.get("type")})

        with app.connection() as connection:
            receiver = app.events.Receiver(connection, handlers={"*": on_event})
            receiver.capture(limit=None, timeout=None, wakeup=True)
