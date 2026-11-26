"""Per-attempt history: Celery retries reuse the task id, so each attempt's
outcome is kept here before the run row is overwritten by the next one."""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from celery_manager.db.run_control import TaskRunAttempt

MAX_TRACEBACK_CHARS = 64_000


def upsert_attempt(session, task_id: str, attempt: int, *, config=None, task_name=None,
                   args=None, kwargs=None, **fields) -> None:
    """Merge `fields` into the `(task_id, attempt)` row (None never
    overwrites). Exception and traceback go through the redaction rules,
    using the run's arguments to find secrets. The caller commits."""
    from celery_manager.redaction import redact_fields
    if fields.get("exception") is not None or fields.get("traceback") is not None:
        redacted = redact_fields(config, task_name, {"args": args, "kwargs": kwargs,
                                                     "exception": fields.get("exception"),
                                                     "traceback": fields.get("traceback")})
        fields["exception"], fields["traceback"] = redacted.get("exception"), redacted.get("traceback")
    if fields.get("traceback"):
        fields["traceback"] = fields["traceback"][-MAX_TRACEBACK_CHARS:]
    for _ in range(2):
        row = session.query(TaskRunAttempt).filter_by(task_id=task_id, attempt=attempt).one_or_none()
        if row is None:
            row = TaskRunAttempt(task_id=task_id, attempt=attempt, status=fields.get("status") or "started")
            session.add(row)
        for key, value in fields.items():
            if value is not None:
                setattr(row, key, value)
        try:
            with session.begin_nested():
                session.flush()
            return
        except IntegrityError:
            session.expunge(row)  # the other writer's row wins; merge into it
    raise RuntimeError(f"attempt {attempt} of {task_id} could not be stored")


def list_attempts(session, task_id: str) -> list[TaskRunAttempt]:
    return session.query(TaskRunAttempt).filter_by(task_id=task_id).order_by(TaskRunAttempt.attempt).all()
