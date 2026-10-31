from __future__ import annotations

import functools
import json
import logging
import traceback as tb_module
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from celery import Task
from celery.exceptions import Retry

from celery_manager import progress as progress_module
from celery_manager.logcapture import capture_logs
from celery_manager.occurrences import (
    CALENDAR_MATCH_WINDOW,
    INTERVAL_HEADER,
    OCCURRENCE_HEADER,
    SCHEDULE_HEADER,
    SENT_AT_HEADER,
    parse_occurrence,
)

logger = logging.getLogger(__name__)

_session_factory = None
_eta_tolerance_seconds = 60
_warned_unconfigured = False
_warned_legacy_match = False
# Set once this process has seen evidence that beat stamps occurrence
# headers (a stamped message, or a stamped `task_runs` row). From then on a
# message without headers never consumes an override.
_beat_stamps = False
_config = None


@dataclass
class _RuntimeState:
    sessions: object
    tolerance: int
    config: object = None
    beat_stamps: bool = False
    warned_legacy: bool = False
    warned_unconfigured: bool = False


_runtime_context = ContextVar("cm_task_runtime", default=None)


def _state_for_app(app):
    state = getattr(app, "_cm_runtime_state", None)
    if state is None:
        state = getattr(app, "_cm_unconfigured_state", None)
        if state is None:
            state = _RuntimeState(None, 60)
            if app is not None:
                app._cm_unconfigured_state = state
    return state


def _active_state():
    state = _runtime_context.get()
    if state is not None:
        return state
    # Direct configure(factory) remains the legacy process-wide integration.
    if _config is None or _session_factory is None:
        return None
    from celery import current_task
    task = current_task._get_current_object()
    if task is not None:
        return _state_for_app(task.app)
    return None


def _sessions():
    state = _active_state()
    return state.sessions if state is not None else _session_factory


def _settings():
    state = _active_state()
    return state.config if state is not None else _config


def _with_task_runtime(function):
    @functools.wraps(function)
    def scoped(*args, **kwargs):
        if _config is None or _session_factory is None:
            state = None
        else:
            from celery import current_task
            task = args[0] if args and isinstance(args[0], Task) else current_task._get_current_object()
            state = _state_for_app(getattr(task, "app", None))
        token = _runtime_context.set(state)
        try:
            return function(*args, **kwargs)
        finally:
            _runtime_context.reset(token)
    return scoped


def configure(session_factory, eta_tolerance_seconds: int = 60, config=None) -> None:
    """Wire the decorator to a session factory.

    Called once per *process* that runs `@track`-decorated tasks — which
    means the Celery worker process too, not just the API process.
    `celery_manager.setup(config)` does this for you (and is idempotent),
    so prefer that over calling this directly.
    """
    global _session_factory, _eta_tolerance_seconds, _warned_unconfigured, _warned_legacy_match, _beat_stamps
    global _config
    _config = config
    _session_factory = session_factory
    _eta_tolerance_seconds = eta_tolerance_seconds
    _warned_unconfigured = False
    _warned_legacy_match = False
    _beat_stamps = False
    if config is not None and config.celery_app is not None:
        previous = getattr(config.celery_app, "_cm_runtime_state", None)
        if previous is None or previous.sessions is not session_factory or previous.config is not config:
            config.celery_app._cm_runtime_state = _RuntimeState(session_factory, eta_tolerance_seconds, config)


def _warn_unconfigured_once(task_name: str) -> None:
    """A `@track`ed task running in a process that never called
    `celery_manager.setup()` silently loses pause/skip and log capture.
    That is almost always a misconfigured worker rather than an intentional
    choice, so say so — once, at WARNING — instead of failing silently.
    Deliberately does not raise: an observability gap must never take down
    the host application's actual task."""
    global _warned_unconfigured
    state = _active_state()
    if state is not None:
        if state.warned_unconfigured:
            return
        state.warned_unconfigured = True
    else:
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


@dataclass(frozen=True)
class BeatOccurrence:
    """The occurrence identity beat stamped on this message (see
    `celery_manager.beat`)."""

    schedule_name: str
    occurrence: datetime
    interval_seconds: float | None = None
    sent_at: datetime | None = None


def _header(request, name: str):
    """A custom message header from the task request. A real worker exposes
    message headers as request attributes; eager `apply(headers=...)` puts
    them in `request.headers`."""
    value = getattr(request, name, None)
    if value is None:
        headers = getattr(request, "headers", None)
        if isinstance(headers, dict):
            value = headers.get(name)
    return value


def _beat_occurrence(task_self) -> BeatOccurrence | None:
    """Read the beat occurrence headers off the running task, or None if the
    message has none (a manual call, a retry, or a non-stamping beat).
    Never raises."""
    try:
        request = getattr(task_self, "request", None)
        if request is None:
            from celery import current_task

            request = getattr(current_task, "request", None)
        if request is None:
            return None
        schedule_name = _header(request, SCHEDULE_HEADER)
        occurrence = parse_occurrence(_header(request, OCCURRENCE_HEADER))
        if not isinstance(schedule_name, str) or not schedule_name or occurrence is None:
            return None
        interval = _header(request, INTERVAL_HEADER)
        try:
            interval = float(interval) if interval is not None else None
        except (TypeError, ValueError):
            interval = None
        _mark_beat_stamping()
        return BeatOccurrence(
            schedule_name=schedule_name,
            occurrence=occurrence,
            interval_seconds=interval if interval and interval > 0 else None,
            sent_at=parse_occurrence(_header(request, SENT_AT_HEADER)),
        )
    except Exception:  # noqa: BLE001 — a malformed header must not fail the task
        return None


def _mark_beat_stamping() -> None:
    global _beat_stamps
    state = _active_state()
    if state is not None:
        state.beat_stamps = True
    else:
        _beat_stamps = True


def _beat_is_stamping(session) -> bool:
    """True once any run in storage carries a beat occurrence stamp. Cached
    per process once true (a beat that stamps keeps stamping)."""
    global _beat_stamps
    state = _active_state()
    if state.beat_stamps if state is not None else _beat_stamps:
        return True
    from celery_manager.db.models import TaskRun

    if session.query(TaskRun.id).filter(TaskRun.occurrence_at.isnot(None)).first() is not None:
        if state is not None:
            state.beat_stamps = True
        else:
            _beat_stamps = True
    return state.beat_stamps if state is not None else _beat_stamps


def _warn_legacy_match_once(schedule_name: str) -> None:
    global _warned_legacy_match
    state = _active_state()
    if state is not None:
        if state.warned_legacy:
            return
        state.warned_legacy = True
    else:
        if _warned_legacy_match:
            return
        _warned_legacy_match = True
    logger.warning(
        "celery-manager: matched a schedule override for %r by wall-clock time "
        "because the message carries no occurrence headers. Run beat with "
        "`-S celery_manager.beat:Scheduler` (or the StampOccurrencesMixin) so a "
        "late start or a manual call near the target time cannot skip the "
        "wrong run. (Logged once per process.)",
        schedule_name,
    )


def _closest(candidates, t: datetime, window: timedelta):
    best = None
    best_diff = None
    for override in candidates:
        target_time = override.target_time
        if target_time.tzinfo is None:
            # SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
            # columns; values are always written as UTC, so treat naive as UTC.
            target_time = target_time.replace(tzinfo=timezone.utc)
        diff = abs(target_time - t)
        if diff <= window and (best is None or diff < best_diff):
            best, best_diff = override, diff
    return best


def _find_matching_override(session, schedule_name: str, occurrence: BeatOccurrence | None = None):
    """The unconsumed override this run should apply, or None.

    With beat occurrence headers (the normal case once beat runs the
    stamping scheduler) the override must target that occurrence: exactly
    for crontab/solar (`CALENDAR_MATCH_WINDOW` only absorbs millisecond
    rounding), and within `eta_tolerance_seconds` (capped at half the
    period) for interval schedules, whose real firing times move by beat's
    scheduling jitter from one firing to the next. How late the task starts
    does not matter.

    Without headers: if beat is known to stamp (`_beat_is_stamping`), the
    message is a manual call or a retry and never consumes an override.
    Otherwise (an older beat, or beat without the scheduler) we keep the
    previous behaviour: an override within `eta_tolerance_seconds` of *now*.
    """
    from celery_manager.db.models import ScheduleOverride

    candidates = (
        session.query(ScheduleOverride)
        .filter_by(schedule_name=schedule_name, consumed=False)
        .all()
    )
    if not candidates:
        return None

    tolerance = timedelta(seconds=_active_state().tolerance if _active_state() is not None else _eta_tolerance_seconds)
    if occurrence is not None:
        if occurrence.interval_seconds is not None:
            window = min(tolerance, timedelta(seconds=occurrence.interval_seconds / 2))
        else:
            window = CALENDAR_MATCH_WINDOW
        return _closest(candidates, occurrence.occurrence, window)

    if _beat_is_stamping(session):
        return None
    match = _closest(candidates, datetime.now(timezone.utc), tolerance)
    if match is not None:
        _warn_legacy_match_once(schedule_name)
    return match


def _schedule_is_paused(session, schedule_name: str) -> bool:
    """True if the whole schedule is paused (`pause_schedule`). Unknown
    names — a `@track`ed task that is not in `beat_schedule` at all, or one
    whose `schedule_name` doesn't match a beat key — are never paused."""
    from celery_manager.control.schedules import expire_stale_pause
    from celery_manager.db.models import PeriodicSchedule

    row = (
        session.query(PeriodicSchedule.paused_at, PeriodicSchedule.paused_until)
        .filter_by(name=schedule_name)
        .one_or_none()
    )
    if row is None or row.paused_at is None:
        return False
    if row.paused_until is None:
        return True
    until = row.paused_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) < until:
        return True
    # `pause_schedule(until=...)` has elapsed: firings run normally again. Clear
    # the stale pause now (audited once, however many workers get here).
    expire_stale_pause(session, schedule_name, row.paused_until)
    return False


def _maintenance_window_reason(session, schedule_name: str, at: datetime) -> str | None:
    """The skip reason if `at` (the firing's occurrence time) falls inside an
    enabled maintenance window that applies to this schedule, else None.
    Names every window when several overlap."""
    from celery_manager.control.maintenance import active_windows

    windows = active_windows(session, schedule_name, at)
    if not windows:
        return None
    return "maintenance window: " + ", ".join(w.name for w in windows)


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

    Race-safe: the get-or-create goes through `db.runs.lock_task_run`, which
    tolerates the listener (or `retry_task`) inserting the same uuid between
    our check and our commit. A plain SELECT-then-add here only *narrowed*
    that window — the `started` row is written at the same moment the
    worker emits task-started, so losing it was routine under a real worker.
    The seed is create_defaults overlaid with our own fields, matching what
    the old insert path produced.

    Status precedence: the decorator's status is applied unconditionally,
    unlike the listener's. Its writes happen in-process, in order, around
    the body (started, then the outcome), so there is no stale decorator
    write to guard against; events, by contrast, arrive from several
    producers out of order — see `listener._accepts_status`.
    """
    from celery_manager.db.runs import lock_task_run

    run = lock_task_run(session, task_id, seed={**(create_defaults or {}), **fields})
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
        with _sessions()() as session:
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


def _workflow_lineage(task_self) -> dict:
    """`parent_id` / `root_id` / `group_id` from the running task's request.

    Celery sets these on `request` for chain/group/chord members. Missing or
    falsy values come back as None, which `_upsert_task_run` skips, so a
    standalone task writes nothing and a listener-captured value is kept.
    Never raises: lineage is a nicety, not worth failing a task over.
    """
    request = getattr(task_self, "request", None)
    if request is None:
        try:
            from celery import current_task

            request = getattr(current_task, "request", None)
        except Exception:  # noqa: BLE001 — current_task is a proxy; never trust it
            request = None
    lineage = {}
    for field, attr in (("parent_id", "parent_id"), ("root_id", "root_id"), ("group_id", "group")):
        value = getattr(request, attr, None)
        lineage[field] = value if isinstance(value, str) and value else None
    return lineage


def _initiator(task_self):
    request = getattr(task_self, "request", None)
    if request is None:
        from celery import current_task
        request = getattr(current_task, "request", None)
    value = _header(request, "cm_initiator")
    return value if isinstance(value, str) and 0 < len(value) <= 255 else None


def track(schedule_name: str | None = None, *, schedule_from=None, consume_kwargs: tuple[str, ...] = (),
          singleton=None, singleton_ttl: float = 1800, on_conflict: str = "skip"):
    """Decorator for Celery tasks. Apply directly below @app.task:

        @app.task(name="demo.echo", bind=True)
        @track(schedule_name="demo-every-2h")
        def echo(self, value):
            ...

    Checks for a matching schedule_overrides entry before running; if found
    and action is skip/cancel, records the run as 'skipped' and does not
    call the wrapped function. Does the same if the whole schedule is
    paused (`PeriodicSchedule.paused_at` set via `pause_schedule`) or the
    firing falls inside an enabled maintenance window
    (`celery_manager.control.maintenance`); the run's `skip_reason` says
    which. Otherwise it writes a `started` row, runs the
    task with log capture on, and rewrites that row with the outcome —
    `succeeded`, `failed`, or `retried` if the body raised Celery's `Retry`
    to schedule another attempt.

    Requires `celery_manager.setup(config)` to have been called in this
    process (the worker process, for tasks that actually execute there).
    Without it the wrapper degrades to a pass-through — the task body still
    runs, but with no pause/skip check and no log capture.
    """

    if singleton is not None and singleton is not False:
        from celery_manager.locks import ttl_ms
        ttl_ms(singleton_ttl)
        if on_conflict not in ("skip", "retry"):
            raise ValueError("on_conflict must be 'skip' or 'retry'")
        if singleton is not True and not isinstance(singleton, str) and not callable(singleton):
            raise ValueError("singleton must be True, a key string, or a callable")

    def decorator(func):
        if isinstance(func, Task):
            raise TypeError("Apply @track() below @app.task, directly on the task function")
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            task_self, task_id, task_name = _identify_run(args, func)
            extracted = None
            if schedule_from is not None:
                try:
                    extracted = schedule_from(args[1:] if task_self else args, dict(kwargs))
                except Exception:
                    logger.exception("celery-manager: schedule identity extractor failed")
            for key in consume_kwargs:
                kwargs.pop(key, None)
            effective_schedule_name = extracted or schedule_name or task_name

            if _sessions() is None:
                # Not configured (e.g. task called outside a configured app) — run normally.
                _warn_unconfigured_once(task_name)
                return func(*args, **kwargs)

            run_args = _safe_json(args[1:] if task_self else args)
            run_kwargs = _safe_json(kwargs)

            # Which beat firing this is, if beat stamped it. The stamped
            # entry name is what overrides and pauses are keyed by, so it
            # wins over `schedule_name` for those checks.
            beat_occurrence = _beat_occurrence(task_self)
            control_schedule_name = beat_occurrence.schedule_name if beat_occurrence else effective_schedule_name
            occurrence_fields = {
                "occurrence_at": beat_occurrence.occurrence if beat_occurrence else None,
                "beat_sent_at": beat_occurrence.sent_at if beat_occurrence else None,
            }

            # Skip-check. Guarded for the same reason as _persist_run: if
            # storage is unreachable we fail *open* (run the task normally),
            # matching the documented degrade-gracefully behaviour above,
            # rather than raising out of the wrapper and failing a task that
            # celery-manager was only meant to observe.
            skipped = False
            skip_reason: str | None = None
            try:
                with _sessions()() as session:
                    override = _find_matching_override(session, control_schedule_name, beat_occurrence)
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
                        skip_reason = "occurrence skipped by an operator"
                    # Whole-schedule pause. Checked *after* the occurrence
                    # override on purpose: an override aimed at this firing
                    # is consumed whether or not the schedule is also paused.
                    # The firing it targeted has happened (and is being
                    # skipped either way); leaving it unconsumed would keep a
                    # stale row in the pool that `pause_occurrence`'s
                    # tolerance matching and the occurrences view would have
                    # to keep stepping around.
                    if not skipped and _schedule_is_paused(session, control_schedule_name):
                        skipped = True
                        skip_reason = "schedule paused"
                    # Maintenance windows last, by the same code path: the
                    # firing is judged at its scheduled time (the stamped
                    # occurrence, so a late-delivered message is still
                    # judged by when it was due), or now when unstamped.
                    if not skipped:
                        at = beat_occurrence.occurrence if beat_occurrence else datetime.now(timezone.utc)
                        skip_reason = _maintenance_window_reason(session, control_schedule_name, at)
                        skipped = skip_reason is not None
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
                    skip_reason=skip_reason,
                    **occurrence_fields,
                initiator=_initiator(task_self),
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
                result_truncated=False,
                log_output_truncated=False,
                log_output="",
                args=run_args,
                kwargs=run_kwargs,
                schedule_name=effective_schedule_name,
                started_at=started_at,
                **occurrence_fields,
                initiator=_initiator(task_self),
                **_workflow_lineage(task_self),
                clear=(
                    "exception", "traceback", "finished_at", "runtime_seconds",
                    "progress_current", "progress_total", "progress_message",
                    "progress_updated_at", "result_repr",
                ),
            )

            with progress_module._run_context(effective_task_id), capture_logs(_settings()) as buffer:
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
                        log_output_truncated=buffer.truncated,
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
                        log_output_truncated=buffer.truncated,
                    )
                    raise
                else:
                    finished_at = datetime.now(timezone.utc)
                    from celery_manager.logcapture import result_fields
                    output = result_fields(result, _settings()) if _settings() is None or _settings().store_results else {}
                    _persist_run(
                        effective_task_id,
                        **output,
                        task_name=task_name,
                        status="succeeded",
                        args=run_args,
                        kwargs=run_kwargs,
                        schedule_name=effective_schedule_name,
                        started_at=started_at,
                        finished_at=finished_at,
                        runtime_seconds=(finished_at - started_at).total_seconds(),
                        log_output=buffer.getvalue(),
                        log_output_truncated=buffer.truncated,
                    )
                    return result

        controlled = wrapper
        if singleton is not None and singleton is not False:
            controlled = _singleton_wrapper(wrapper, func, singleton, singleton_ttl, on_conflict, consume_kwargs)
        return _with_task_runtime(controlled)

    return decorator


def _singleton_wrapper(wrapped, original, key_spec, ttl, on_conflict, consume_kwargs):
    @functools.wraps(wrapped)
    def controlled(*args, **kwargs):
        from celery_manager.locks import (
            SingletonConflict,
            SingletonUnavailable,
            provider_for,
            validate_key,
        )
        from celery_manager.singleton import maintain_lease
        task, task_id, task_name = _identify_run(args, original)
        if not task_id:
            raise SingletonUnavailable("singleton tasks must execute through Celery with a request id")
        if task is None:
            from celery import current_task
            task = current_task._get_current_object()
        user_args = args[1:] if args and isinstance(args[0], Task) else args
        try:
            # Match the task body's public argument contract; routing-only
            # schedule metadata is consumed by the tracking wrapper below.
            key_kwargs = {key: value for key, value in kwargs.items() if key not in consume_kwargs}
            key = task_name if key_spec is True else key_spec(*user_args, **key_kwargs) if callable(key_spec) else key_spec
            validate_key(key)
            provider = provider_for(_settings())
            acquired, lease = provider.acquire(key, task_id, task_name, ttl)
            if not acquired:
                reason = f"singleton lock held by task {lease.task_id} ({lease.task_name})"
                if on_conflict == "retry":
                    _persist_run(task_id, task_name=task_name, status="retried", exception=reason)
                    raise task.retry(exc=SingletonConflict(reason), countdown=min(ttl / 3, 60))
                _persist_run(task_id, task_name=task_name, status="skipped", skip_reason=reason[:255],
                             args=_safe_json(user_args), kwargs=_safe_json(kwargs),
                             finished_at=datetime.now(timezone.utc))
                return None
            with maintain_lease(provider, lease, ttl):
                return wrapped(*args, **kwargs)
        except Retry:
            raise
        except Exception as exc:
            # Explicit singleton enforcement is fail-closed, unlike ordinary
            # observability. A lock outage must never run an unguarded body.
            _persist_run(task_id, task_name=task_name, status="failed", exception=_describe_exception(exc)[0],
                         finished_at=datetime.now(timezone.utc))
            raise
    return controlled
