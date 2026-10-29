from datetime import datetime, timezone

from celery_manager.control.audit import record
from celery_manager.control.tasks import TaskRunNotFound, get_task_run
from celery_manager.db.models import FailureResolution, FailureResolutionHistory
from celery_manager.db.runs import lock_task_run


class FailureStateConflict(ValueError):
    pass


def resolve_failure(session, task_id, state, actor, note=None):
    if state not in ("open", "acknowledged", "ignored"):
        raise ValueError("Failure state must be open, acknowledged or ignored")
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise ValueError("A resolution note must contain at most 2000 characters")
    run = get_task_run(session, task_id)
    if run is None:
        raise TaskRunNotFound(task_id)
    run = lock_task_run(session, task_id, seed={"task_name": run.task_name, "status": run.status})
    if run.status != "failed":
        raise FailureStateConflict("Only failed runs have an inbox resolution")
    current = session.get(FailureResolution, task_id)
    if current is not None and current.state == state and current.note == note:
        return current
    now = datetime.now(timezone.utc)
    if current is None:
        current = FailureResolution(task_id=task_id)
        session.add(current)
    current.state, current.actor, current.note, current.updated_at = state, str(actor), note, now
    session.add(FailureResolutionHistory(task_id=task_id, state=state, actor=str(actor), note=note,
                                         actor_subject=getattr(actor, "subject", None), created_at=now))
    # Audit and review state share one transaction.
    record(session, actor, "failure." + state, "task", task_id, {"state": state, "note": note})
    return current


def resolution_history(session, task_id):
    return session.query(FailureResolutionHistory).filter_by(task_id=task_id).order_by(FailureResolutionHistory.id).all()
