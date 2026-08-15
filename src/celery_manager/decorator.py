from __future__ import annotations

import functools
import json
import traceback as tb_module
from datetime import datetime, timedelta, timezone

from celery_manager.logcapture import capture_logs

_session_factory = None
_eta_tolerance_seconds = 60


def configure(session_factory, eta_tolerance_seconds: int = 60) -> None:
    """Wire the decorator to a session factory. Called once at app setup
    (celery_manager.setup(config) does this for you in the core API)."""
    global _session_factory, _eta_tolerance_seconds
    _session_factory = session_factory
    _eta_tolerance_seconds = eta_tolerance_seconds


def _find_matching_override(session, schedule_name: str):
    from celery_manager.db.models import ScheduleOverride

    now = datetime.now(timezone.utc)
    window = timedelta(seconds=_eta_tolerance_seconds)
    candidates = (
        session.query(ScheduleOverride)
        .filter_by(schedule_name=schedule_name, consumed=False)
        .all()
    )
    for override in candidates:
        target_time = override.target_time
        if target_time.tzinfo is None:
            # SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
            # columns; values are always written as UTC, so treat naive as UTC.
            target_time = target_time.replace(tzinfo=timezone.utc)
        if abs(target_time - now) <= window:
            return override
    return None


def track(schedule_name: str | None = None):
    """Decorator for Celery tasks. Apply above @app.task:

        @app.task(name="demo.echo", bind=True)
        @track(schedule_name="demo-every-2h")
        def echo(self, value):
            ...

    Checks for a matching schedule_overrides entry before running; if found
    and action is skip/cancel, records the run as 'skipped' and does not
    call the wrapped function. Otherwise runs normally, capturing logs and
    persisting a TaskRun row with the outcome.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            from celery_manager.db.models import TaskRun

            task_self = args[0] if args else None
            task_id = getattr(task_self.request, "id", None) if task_self else None
            task_name = getattr(task_self, "name", func.__name__) if task_self else func.__name__
            effective_schedule_name = schedule_name or task_name

            if _session_factory is None:
                # Not configured (e.g. task called outside a configured app) — run normally.
                return func(*args, **kwargs)

            with _session_factory() as session:
                override = _find_matching_override(session, effective_schedule_name)
                if override is not None and override.action in ("skip", "cancel"):
                    override.consumed = True
                    session.add(TaskRun(
                        task_id=task_id or f"skipped-{datetime.now(timezone.utc).isoformat()}",
                        task_name=task_name,
                        status="skipped",
                        args=json.dumps(args[1:] if task_self else args),
                        kwargs=json.dumps(kwargs),
                        schedule_name=effective_schedule_name,
                        started_at=datetime.now(timezone.utc),
                        finished_at=datetime.now(timezone.utc),
                    ))
                    session.commit()
                    return None

            started_at = datetime.now(timezone.utc)
            with capture_logs() as buffer:
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    finished_at = datetime.now(timezone.utc)
                    with _session_factory() as session:
                        session.add(TaskRun(
                            task_id=task_id or f"run-{started_at.isoformat()}",
                            task_name=task_name,
                            status="failed",
                            args=json.dumps(args[1:] if task_self else args),
                            kwargs=json.dumps(kwargs),
                            schedule_name=effective_schedule_name,
                            started_at=started_at,
                            finished_at=finished_at,
                            runtime_seconds=(finished_at - started_at).total_seconds(),
                            exception=str(exc),
                            traceback=tb_module.format_exc(),
                            log_output=buffer.getvalue(),
                        ))
                        session.commit()
                    raise
                else:
                    finished_at = datetime.now(timezone.utc)
                    with _session_factory() as session:
                        session.add(TaskRun(
                            task_id=task_id or f"run-{started_at.isoformat()}",
                            task_name=task_name,
                            status="succeeded",
                            args=json.dumps(args[1:] if task_self else args),
                            kwargs=json.dumps(kwargs),
                            schedule_name=effective_schedule_name,
                            started_at=started_at,
                            finished_at=finished_at,
                            runtime_seconds=(finished_at - started_at).total_seconds(),
                            log_output=buffer.getvalue(),
                        ))
                        session.commit()
                    return result

        return wrapper

    return decorator
