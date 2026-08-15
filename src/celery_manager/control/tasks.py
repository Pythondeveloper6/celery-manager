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
