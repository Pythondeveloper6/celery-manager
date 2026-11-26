"""Cooperative cancel, read from inside a running task.

    for i, row in enumerate(rows):
        if report_progress(i, len(rows)):   # True once a cancel was requested
            break
        ...
    raise_if_cancelled()                    # or stop with TaskCancelled

A run that ends with `TaskCancelled` is recorded as `cancelled`, not failed.
"""
from __future__ import annotations

import threading
import time

_checked: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()


class TaskCancelled(Exception):
    """Raised by `raise_if_cancelled()`; the run is recorded as `cancelled`."""


def cancel_requested() -> bool:
    """True once an operator asked this run to stop. Re-reads the flag at
    most every `cancel_check_interval_seconds`; False outside a task or when
    the flag cannot be read. Never raises."""
    try:
        from celery_manager import decorator
        from celery_manager.progress import _resolve_run

        task_id, _ = _resolve_run()
        sessions = decorator._sessions()
        if not task_id or sessions is None:
            return False
        interval = getattr(decorator._settings(), "cancel_check_interval_seconds", 2.0)
        now = time.monotonic()
        with _lock:
            cached = _checked.get(task_id)
        if cached is not None and (cached[1] or now - cached[0] < interval):
            return cached[1]
        from celery_manager.db.models import TaskRun
        from celery_manager.telemetry import guarded
        with guarded(decorator._settings()), sessions() as session:
            value = session.query(TaskRun.cancel_requested_at).filter_by(task_id=task_id).scalar() is not None
        with _lock:
            if len(_checked) > 10_000:
                _checked.clear()
            _checked[task_id] = (now, value)
        return value
    except Exception:  # noqa: BLE001 - a failed read must not fail the task
        return False


def raise_if_cancelled() -> None:
    if cancel_requested():
        raise TaskCancelled("cancel requested")


def forget(task_id: str) -> None:
    with _lock:
        _checked.pop(task_id, None)
