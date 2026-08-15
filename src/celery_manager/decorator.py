from __future__ import annotations

import functools
import json
import logging
import traceback as tb_module
from datetime import datetime, timedelta, timezone

from celery_manager.logcapture import capture_logs

logger = logging.getLogger(__name__)

_session_factory = None
_eta_tolerance_seconds = 60
_warned_unconfigured = False


def configure(session_factory, eta_tolerance_seconds: int = 60) -> None:
    """Wire the decorator to a session factory.

    Called once per *process* that runs `@track`-decorated tasks — which
    means the Celery worker process too, not just the API process.
    `celery_manager.setup(config)` does this for you (and is idempotent),
    so prefer that over calling this directly.
    """
    global _session_factory, _eta_tolerance_seconds, _warned_unconfigured
    _session_factory = session_factory
    _eta_tolerance_seconds = eta_tolerance_seconds
    _warned_unconfigured = False


def _warn_unconfigured_once(task_name: str) -> None:
    """A `@track`ed task running in a process that never called
    `celery_manager.setup()` silently loses pause/skip and log capture.
    That is almost always a misconfigured worker rather than an intentional
    choice, so say so — once, at WARNING — instead of failing silently.
    Deliberately does not raise: an observability gap must never take down
    the host application's actual task."""
    global _warned_unconfigured
    if _warned_unconfigured:
        return
    _warned_unconfigured = True
    logger.warning(
        "celery-manager: @track task %r ran in a process that never called "
        "celery_manager.setup(config) — schedule pause/skip and log capture "
        "are inactive here. Call celery_manager.setup(config) at import time "
        "in your Celery worker's task module. (Logged once per process.)",
        task_name,
    )


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


def _upsert_task_run(session, task_id: str, **fields) -> None:
    """Merge this run's decorator-owned facts into the `task_runs` row for
    `task_id`, creating the row only if nobody else has.

    `task_runs.task_id` is UNIQUE, and in the documented deployment topology
    (spec §4/§5) the events listener runs as its own process alongside the
    worker — so by the time a decorated task body finishes, the listener may
    already have inserted a row for this same UUID from `task-sent` /
    `task-started`. Blindly `session.add(TaskRun(...))` in that situation
    raises `IntegrityError`, which (empirically, in eager mode and in a real
    worker alike) surfaces as the *task* failing even though its body
    succeeded. Hence upsert, not insert.

    Merge, don't overwrite: per spec §4.1/§5 the events listener is the
    source of truth for the coarse lifecycle fields it owns (`worker`,
    `queue`, and the queued/started transitions), while the decorator owns
    `log_output`, `exception`, `traceback`, and the *final* status transition
    (skipped, or the real succeeded/failed outcome). We therefore only write
    the fields the decorator actually knows about, and skip any whose value
    is `None`, so a decorator write can never null out a value the listener
    established.
    """
    from celery_manager.db.models import TaskRun

    run = session.query(TaskRun).filter_by(task_id=task_id).one_or_none()
    if run is None:
        run = TaskRun(task_id=task_id)
        session.add(run)
    for key, value in fields.items():
        if value is None:
            continue
        setattr(run, key, value)


def _persist_run(task_id: str, **fields) -> None:
    """Persist one run outcome, swallowing any storage failure.

    Spec §4.1: celery-manager is an observability/control layer wrapped
    around someone else's task. It must never be the reason a task's
    reported outcome changes — a failed write here must not turn a
    successful task into a reported failure, and must not swallow or
    displace a real task exception on the failure path. So every storage
    error is logged and dropped; the caller's control flow is unaffected.
    """
    try:
        with _session_factory() as session:
            _upsert_task_run(session, task_id, **fields)
            session.commit()
    except Exception:  # deliberate: storage must never affect the task's outcome
        logger.exception(
            "celery-manager: could not persist the task_runs row for %s; "
            "the task's own outcome is unaffected.",
            task_id,
        )


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

    Requires `celery_manager.setup(config)` to have been called in this
    process (the worker process, for tasks that actually execute there).
    Without it the wrapper degrades to a pass-through — the task body still
    runs, but with no pause/skip check and no log capture.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            task_self = args[0] if args else None
            task_id = getattr(task_self.request, "id", None) if task_self else None
            task_name = getattr(task_self, "name", func.__name__) if task_self else func.__name__
            effective_schedule_name = schedule_name or task_name

            if _session_factory is None:
                # Not configured (e.g. task called outside a configured app) — run normally.
                _warn_unconfigured_once(task_name)
                return func(*args, **kwargs)

            run_args = json.dumps(args[1:] if task_self else args)
            run_kwargs = json.dumps(kwargs)

            # Skip-check. Guarded for the same reason as _persist_run: if
            # storage is unreachable we fail *open* (run the task normally),
            # matching the documented degrade-gracefully behaviour above,
            # rather than raising out of the wrapper and failing a task that
            # celery-manager was only meant to observe.
            skipped = False
            try:
                with _session_factory() as session:
                    override = _find_matching_override(session, effective_schedule_name)
                    if override is not None and override.action in ("skip", "cancel"):
                        # Commit the "this occurrence is consumed" decision on
                        # its own, before writing the task_runs row. The two
                        # are independent facts, and consuming the override is
                        # the one that must not be lost: if it were rolled back
                        # by a later task_runs failure, the same override would
                        # match again and silently skip a *later*, unrelated
                        # run of this schedule.
                        override.consumed = True
                        session.commit()
                        skipped = True
            except Exception:  # deliberate fail-open: see _persist_run's docstring
                logger.exception(
                    "celery-manager: schedule-override check failed for %r; "
                    "running the task normally.",
                    effective_schedule_name,
                )

            if skipped:
                now = datetime.now(timezone.utc)
                _persist_run(
                    task_id or f"skipped-{now.isoformat()}",
                    task_name=task_name,
                    status="skipped",
                    args=run_args,
                    kwargs=run_kwargs,
                    schedule_name=effective_schedule_name,
                    started_at=now,
                    finished_at=now,
                )
                return None

            started_at = datetime.now(timezone.utc)
            effective_task_id = task_id or f"run-{started_at.isoformat()}"
            with capture_logs() as buffer:
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    finished_at = datetime.now(timezone.utc)
                    _persist_run(
                        effective_task_id,
                        task_name=task_name,
                        status="failed",
                        args=run_args,
                        kwargs=run_kwargs,
                        schedule_name=effective_schedule_name,
                        started_at=started_at,
                        finished_at=finished_at,
                        runtime_seconds=(finished_at - started_at).total_seconds(),
                        exception=str(exc),
                        traceback=tb_module.format_exc(),
                        log_output=buffer.getvalue(),
                    )
                    raise
                else:
                    finished_at = datetime.now(timezone.utc)
                    _persist_run(
                        effective_task_id,
                        task_name=task_name,
                        status="succeeded",
                        args=run_args,
                        kwargs=run_kwargs,
                        schedule_name=effective_schedule_name,
                        started_at=started_at,
                        finished_at=finished_at,
                        runtime_seconds=(finished_at - started_at).total_seconds(),
                        log_output=buffer.getvalue(),
                    )
                    return result

        return wrapper

    return decorator
