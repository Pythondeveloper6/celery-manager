"""Core Python API for task and schedule control (spec §4.3).

Re-exported here so the operations are reachable as
`celery_manager.control.<name>` — the surface spec §4 describes — without
callers needing to know which submodule each one lives in. Every function
takes an explicit SQLAlchemy session and/or the host Celery app, so they are
usable from a shell, a management command, or your own code, not just from
the REST layer.
"""

from celery_manager.control.audit import record
from celery_manager.control.schedules import (
    list_overrides,
    list_periodic_schedules,
    list_upcoming_with_overrides,
    pause_occurrence,
    sync_periodic_schedules,
    upcoming_occurrences,
)
from celery_manager.control.tasks import (
    bulk_retry,
    cancel_task,
    get_task_run,
    list_task_runs,
    retry_task,
    start_task,
)

__all__ = [
    "bulk_retry",
    "cancel_task",
    "get_task_run",
    "list_overrides",
    "list_periodic_schedules",
    "list_task_runs",
    "list_upcoming_with_overrides",
    "pause_occurrence",
    "record",
    "retry_task",
    "start_task",
    "sync_periodic_schedules",
    "upcoming_occurrences",
]
