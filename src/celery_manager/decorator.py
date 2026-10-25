from __future__ import annotations

import functools
import json
import logging
import traceback as tb_module
from datetime import datetime, timedelta, timezone

from celery import Task
from celery.exceptions import Retry

from celery_manager import progress as progress_module
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


def _safe_json(value) -> str:
    """Serialize a run's args/kwargs for `task_runs`, without ever raising.

    `json.dumps` is stricter than Celery is. Celery's own JSON serializer is
    kombu's extended one, which happily round-trips `datetime`, `date`,
    `UUID` and `Decimal` over the broker — so `send_task(kwargs={"since":
    datetime(...)})` is ordinary, supported usage that arrives in the worker
    as a real `datetime`. Feeding that to stdlib `json.dumps` raises
    `TypeError`, and because this call sits *before* the task body, the
    exception used to propagate straight out of the wrapper: the task was
    marked FAILURE and its body never ran at all. Same for a task invoked
    with any non-JSON object under `task_always_eager` or a pickle
    serializer. That is precisely the failure mode spec §4.1 forbids —
    celery-manager deciding a task's outcome.

    `default=repr` keeps the JSON *shape* (a list stays a list, a dict stays
    a dict) and replaces only the leaves stdlib json cannot express, so the
    stored value is still searchable by `q=` and still decodes to something
    of the right type. The outer guard catches what `default` cannot reach —
    non-string dict keys, circular references — and degrades to a repr of the
    whole structure rather than losing the run.
    """
    try:
        return json.dumps(value, default=repr)
    except Exception:  # noqa: BLE001 — serialising must never lose the run
        try:
            return json.dumps(repr(value))
        except Exception:  # noqa: BLE001 — a __repr__ that raises is still not our problem
            return '"<unserializable>"'


def _upsert_task_run(
    session, task_id: str, create_defaults: dict | None = None, clear: tuple[str, ...] = (), **fields
) -> None:
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

    `create_defaults` is for a writer that knows *some* fields only well
    enough to seed a brand-new row, never well enough to overwrite an
    existing one — `report_progress()`, which may be the very first writer to
    touch an undecorated task's uuid and must then supply the NOT NULL
    `task_name`/`status`, but must not restate them over whatever the
    decorator or the listener has since established. Applied only on the
    insert path, and only for non-None values.

    `clear` is the deliberate escape hatch from the skip-None rule, for the
    one caller that genuinely needs to *erase* a field: the `started` write
    at the top of a retry attempt. Celery reuses the task id across attempts,
    so without it the previous attempt's `exception`/`traceback`/progress
    stay on the row forever — a task that failed once and then succeeded
    displayed as `succeeded` with a full traceback attached. Named explicitly
    rather than inferred from a None in `fields`, so that "I don't know this
    field" and "I know this field is now empty" stay different statements.
    """
    from celery_manager.db.models import TaskRun

    run = session.query(TaskRun).filter_by(task_id=task_id).one_or_none()
    if run is None:
        run = TaskRun(task_id=task_id)
        session.add(run)
        for key, value in (create_defaults or {}).items():
            if value is None:
                continue
            setattr(run, key, value)
    for key in clear:
        setattr(run, key, None)
    for key, value in fields.items():
        if value is None:
            continue
        setattr(run, key, value)


def _persist_run(task_id: str, create_defaults: dict | None = None, clear: tuple[str, ...] = (), **fields) -> None:
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
            _upsert_task_run(session, task_id, create_defaults=create_defaults, clear=clear, **fields)
            session.commit()
    except Exception:  # deliberate: storage must never affect the task's outcome
        logger.exception(
            "celery-manager: could not persist the task_runs row for %s; "
            "the task's own outcome is unaffected.",
            task_id,
        )


def _describe_exception(exc: BaseException) -> tuple[str, str]:
    """`(message, traceback)` for a task that raised, without ever raising.

    `str(exc)` looks harmless and is not: an exception class with a custom
    `__str__` that itself raises — a real pattern in database drivers and in
    exception types whose `__init__` skipped `super().__init__` — turned the
    decorator's failure branch into the failure. The `RuntimeError` from
    `__str__` propagated out of the `except` block and *replaced* the task's
    real exception, which is the precise thing `_persist_run`'s docstring
    promises never happens. (`traceback.format_exc` already guards its own
    stringification, but it is cheap to be sure here rather than to depend on
    that.)
    """
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001 — a __str__ that raises must not mask the task error
        message = f"<unprintable {type(exc).__name__}>"
    try:
        formatted = tb_module.format_exc()
    except Exception:  # noqa: BLE001 — recording a failure must never raise
        formatted = f"<unformattable traceback for {type(exc).__name__}>"
    return message, formatted


def _identify_run(args: tuple, func) -> tuple[object | None, str | None, str]:
    """Work out whose run this is: `(task_self, task_id, task_name)`.

    `@track` is documented to sit under `@app.task(bind=True)`, where the
    first positional argument is the Task instance. It used to *assume* that
    and read `args[0].request.id` — so applying it to a task without
    `bind=True` raised `AttributeError: 'str' object has no attribute
    'request'` straight out of the wrapper, before the body ran, failing a
    task over a wiring mistake. Exactly the outcome spec §4.1 forbids, and
    the failure was type-dependent (a first argument that happened to have a
    `.request` attribute would have been silently mistaken for the task
    itself), which is worse than a consistent one.

    So identify the bound Task by type rather than by position, and fall back
    to `celery.current_task` for an unbound task — same guarded lookup, and
    same reasoning, as `progress._resolve_run`. That makes `@track` on an
    unbound task work properly (real name, real uuid, args recorded whole)
    rather than merely not crash. `task_self` is returned as well as used
    here because the caller needs it to know whether `args[0]` is the task
    instance or a real argument.
    """
    task_self = args[0] if args and isinstance(args[0], Task) else None
    if task_self is not None:
        return task_self, getattr(task_self.request, "id", None), getattr(task_self, "name", func.__name__)

    task_id = None
    task_name = func.__name__
    try:
        from celery import current_task

        task_id = getattr(getattr(current_task, "request", None), "id", None)
        task_name = getattr(current_task, "name", None) or func.__name__
    except Exception:  # noqa: BLE001, S110 — deliberate: current_task is a proxy; never trust it
        pass
    return None, task_id, task_name


def track(schedule_name: str | None = None):
    """Decorator for Celery tasks. Apply above @app.task:

        @app.task(name="demo.echo", bind=True)
        @track(schedule_name="demo-every-2h")
        def echo(self, value):
            ...

    Checks for a matching schedule_overrides entry before running; if found
    and action is skip/cancel, records the run as 'skipped' and does not
    call the wrapped function. Otherwise it writes a `started` row, runs the
    task with log capture on, and rewrites that row with the outcome —
    `succeeded`, `failed`, or `retried` if the body raised Celery's `Retry`
    to schedule another attempt.

    Requires `celery_manager.setup(config)` to have been called in this
    process (the worker process, for tasks that actually execute there).
    Without it the wrapper degrades to a pass-through — the task body still
    runs, but with no pause/skip check and no log capture.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            task_self, task_id, task_name = _identify_run(args, func)
            effective_schedule_name = schedule_name or task_name

            if _session_factory is None:
                # Not configured (e.g. task called outside a configured app) — run normally.
                _warn_unconfigured_once(task_name)
                return func(*args, **kwargs)

            run_args = _safe_json(args[1:] if task_self else args)
            run_kwargs = _safe_json(kwargs)

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

            # Announce the run *before* calling the body. Until this existed
            # the decorator only ever wrote when a task finished, so a
            # long-running task was invisible in the dashboard unless the
            # separate events-listener process happened to be running — and
            # `report_progress()` had no row to write to until the run was
            # over, which defeats the point of live progress entirely.
            #
            # Ordering, which is what makes this safe: this write and the
            # terminal write below happen in the same thread, in sequence,
            # around the body, so the final succeeded/failed always lands
            # after (and therefore wins over) this `started`. There is no
            # "late started" window to worry about within the decorator.
            #
            # Against the events listener there is no new hazard either: the
            # listener treats `started` as an ordinary lifecycle status it may
            # overwrite (only `skipped` is in its _DECORATOR_OWNED_STATUSES
            # guard, deliberately), and every event it could apply here —
            # task-started, task-succeeded, task-failed — is emitted by the
            # worker at or after the moment we write this. Note we do not add
            # `started` to that guard: doing so would stop the listener
            # applying the real terminal status to a run whose decorator write
            # failed, which is strictly worse.
            #
            # Retries: Celery reuses the task id across attempts, so attempt
            # N+1 rewrites `started` over the previous attempt's status
            # (correct — the row should show the attempt now in flight). It
            # also has to *erase* what the previous attempt left behind, which
            # is what `clear` below is for. Without it the row kept the last
            # attempt's exception/traceback and progress counters: a task that
            # failed once and then succeeded rendered as `succeeded` with a
            # stale traceback and a stale progress bar, which is a worse lie
            # than showing nothing. finished_at/runtime_seconds are cleared
            # for the same reason — the run is in flight again and has no
            # finish time until it has one.
            #
            # Cost: one extra INSERT/UPDATE+commit per tracked run. Accepted:
            # it is one write against a run that is about to do real work, and
            # it buys visibility of every in-flight task without requiring the
            # listener process at all.
            _persist_run(
                effective_task_id,
                task_name=task_name,
                status="started",
                args=run_args,
                kwargs=run_kwargs,
                schedule_name=effective_schedule_name,
                started_at=started_at,
                clear=(
                    "exception", "traceback", "finished_at", "runtime_seconds",
                    "progress_current", "progress_total", "progress_message",
                    "progress_updated_at",
                ),
            )

            with progress_module._run_context(effective_task_id), capture_logs() as buffer:
                try:
                    result = func(*args, **kwargs)
                except Retry as exc:
                    # `self.retry()` raises `celery.exceptions.Retry`, which
                    # is an ordinary Exception subclass and so used to land in
                    # the failure branch below — recording a run that Celery
                    # considers RETRY as `failed`, with "Retry in 30s: ..." in
                    # the exception column and a finish time for a run that
                    # has not finished. Celery's own vocabulary has a word for
                    # this and so does ours (`_STATUS_BY_EVENT` maps
                    # task-retried -> "retried"), so use it, and leave
                    # finished_at/runtime unset: another attempt is coming and
                    # will rewrite `started` over this.
                    #
                    # Only `Retry` is special-cased. `Ignore`/`Reject` share
                    # its base class but mean "this message is being dropped",
                    # which has no status in our vocabulary — they keep
                    # falling through to `failed`, which at least surfaces
                    # them, rather than being silently reclassified.
                    _persist_run(
                        effective_task_id,
                        task_name=task_name,
                        status="retried",
                        args=run_args,
                        kwargs=run_kwargs,
                        schedule_name=effective_schedule_name,
                        started_at=started_at,
                        exception=_describe_exception(exc)[0],
                        log_output=buffer.getvalue(),
                    )
                    raise
                except Exception as exc:
                    finished_at = datetime.now(timezone.utc)
                    message, formatted = _describe_exception(exc)
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
                        exception=message,
                        traceback=formatted,
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
