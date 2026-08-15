from __future__ import annotations

from celery import Celery

from celery_manager.db.models import TaskRun


def list_task_runs(session, *, status: str | None = None, task_name: str | None = None, limit: int = 50, offset: int = 0) -> list[TaskRun]:
    query = session.query(TaskRun)
    if status is not None:
        query = query.filter_by(status=status)
    if task_name is not None:
        query = query.filter_by(task_name=task_name)
    return query.order_by(TaskRun.id.desc()).offset(offset).limit(limit).all()


def get_task_run(session, task_id: str) -> TaskRun | None:
    return session.query(TaskRun).filter_by(task_id=task_id).one_or_none()


def start_task(celery_app: Celery, task_name: str, args: list, kwargs: dict) -> str:
    """Dispatch a task on demand (e.g. from the dashboard's 'run now' action)."""
    result = celery_app.send_task(task_name, args=args, kwargs=kwargs)
    return result.id


def cancel_task(celery_app: Celery, task_id: str, terminate: bool = False) -> None:
    """Revoke a queued task, or terminate it if already running (terminate=True)."""
    celery_app.control.revoke(task_id, terminate=terminate)


import json as _json

from celery_manager.control.audit import record as _record_audit


def retry_task(celery_app: Celery, session, task_id: str, username: str, args: list | None = None, kwargs: dict | None = None) -> str:
    """Re-run a task, by default with its original args/kwargs; pass
    args/kwargs to override them."""
    run = get_task_run(session, task_id)
    if run is None:
        raise ValueError(f"no task run found for {task_id}")

    final_args = args if args is not None else _json.loads(run.args or "[]")
    final_kwargs = kwargs if kwargs is not None else _json.loads(run.kwargs or "{}")

    new_id = start_task(celery_app, run.task_name, final_args, final_kwargs)
    _record_audit(session, username, "retry", "task", task_id, {"new_task_id": new_id})
    return new_id


def bulk_retry(celery_app: Celery, session, task_ids: list[str], username: str) -> list[str]:
    return [retry_task(celery_app, session, task_id, username) for task_id in task_ids]
