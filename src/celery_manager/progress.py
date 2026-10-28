"""Live progress reporting from inside a running task body.

The dashboard can only show "412 / 10000 rows" if the task itself says so —
Celery's event stream carries lifecycle transitions, not application-level
progress. So this module provides one public function, `report_progress()`,
which a user sprinkles into their own loop::

    @app.task(bind=True)
    @track()
    def import_rows(self, rows):
        for i, row in enumerate(rows):
            handle(row)
            report_progress(i + 1, len(rows), f"row {i + 1}")

It writes straight to the `task_runs` row for the current run (which `@track`
now creates up front, as a `started` row, precisely so progress has somewhere
to land while the task is still in flight).

Two rules govern everything below:

1. **It must never raise into the caller's task.** Same rule as
   `decorator._persist_run`: celery-manager is an observability layer wrapped
   around someone else's work, and a progress write failing — or a user
   passing nonsense — must never change the task's outcome. Every failure
   path here logs and returns.
2. **It must be cheap enough to call in a tight loop.** A naive
   implementation inside a 100k-row loop is 100k commits. Hence the throttle.
"""

from __future__ import annotations

import contextlib
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# At most one DB write per this many seconds, per run. 0.5s is a deliberate
# compromise: fast enough that a dashboard polling every 2-5s never shows a
# stale number, slow enough that a tight loop costs single-digit writes per
# second instead of thousands. Module-level so it is discoverable, tunable by
# an operator with an unusually loaded database, and patchable in tests.
MIN_WRITE_INTERVAL_SECONDS = 0.5

# Progress messages are a one-line UI label ("row 412", "compressing"), not a
# log. Someone will eventually pass a traceback or a whole row payload here,
# and it would then be re-committed on every throttle window. Truncate rather
# than reject: a clipped message is still useful, and rejecting it would lose
# the progress numbers along with it.
MAX_MESSAGE_CHARS = 500

# Set by the `@track` wrapper to the run's *effective* task id (which is not
# always `request.id` — see `decorator.wrapper`, which synthesises an id for
# runs that have none). A ContextVar rather than a module global so that a
# thread/gevent pool running several tasks concurrently in one process keeps
# them separate; the wrapper resets the token in a `finally` so nothing leaks
# into the next task on a reused prefork/thread worker.
_current_task_id: ContextVar[str | None] = ContextVar(
    "celery_manager_current_task_id", default=None
)

# (task_id, monotonic timestamp) of the last write this context actually made.
# The task id is part of the value on purpose: a task that reaches us through
# the `current_task` fallback below never goes through `@track`, so nothing
# resets this var between runs — carrying the id lets a new run recognise the
# previous run's timestamp as not its own instead of inheriting its throttle.
_last_write: ContextVar[tuple[str, float] | None] = ContextVar(
    "celery_manager_progress_last_write", default=None
)

_warned_no_task_id = False
_warned_unconfigured = False


def _warn_no_task_id_once() -> None:
    """`report_progress()` called with no run to attach the progress to —
    typically from a plain function call in a test or a shell, or from a
    helper that runs both inside and outside a task. Say so once, at WARNING,
    and carry on: a progress report is never worth raising over."""
    global _warned_no_task_id
    if _warned_no_task_id:
        return
    _warned_no_task_id = True
    logger.warning(
        "celery-manager: report_progress() was called outside any running "
        "Celery task (no @track context and no celery.current_task), so there "
        "is no task_runs row to record progress against — the call was "
        "ignored. (Logged once per process.)"
    )


def _warn_unconfigured_once() -> None:
    """Mirror of `decorator._warn_unconfigured_once` for the progress path: a
    worker that never called `celery_manager.setup(config)` has no session
    factory, so progress has nowhere to go. Same spirit, same never-raise
    rule; kept separate from the decorator's flag so the message names the
    feature the user is actually missing."""
    global _warned_unconfigured
    if _warned_unconfigured:
        return
    _warned_unconfigured = True
    logger.warning(
        "celery-manager: report_progress() ran in a process that never called "
        "celery_manager.setup(config), so live progress is not being recorded. "
        "Call celery_manager.setup(config) at import time in your Celery "
        "worker's task module. (Logged once per process.)"
    )


def _resolve_run() -> tuple[str | None, str | None]:
    """Find the task id to record progress against, plus the task's name if
    Celery can tell us (only ever used to seed a row we have to create).

    Preference order for the id, and why:

    1. The `@track` ContextVar. `@track` knows the run's *effective* id,
       including the synthetic `run-<timestamp>` one it invents for a run
       that has no `request.id` — and that synthetic id is the one its own
       `task_runs` row is keyed by, so progress must use the same.
    2. `celery.current_task.request.id`, for a task that is genuinely running
       but was never decorated with `@track`. Progress then lands on whatever
       row the events listener created for that uuid — or, if nothing has
       created one yet, on a row we insert ourselves.

    Anything can be missing or a proxy to nothing here, so the whole Celery
    lookup is guarded — a broken/absent Celery context degrades to "no
    progress", never to an exception. `fallback_id` is assigned before
    `task_name` deliberately: if reading the name blows up we still keep the
    id we already have.
    """
    tracked_id = _current_task_id.get()
    if tracked_id:
        # Short-circuit: this is the hot path (once per loop iteration, before
        # the throttle can drop the call), and touching Celery's thread-local
        # proxy here would buy nothing — `@track` has already created the row
        # with the real task name, and if that write happened to fail, the
        # "unknown" seed below is corrected by the terminal write anyway.
        return tracked_id, None

    fallback_id = None
    task_name = None
    try:
        from celery import current_task

        request = getattr(current_task, "request", None)
        fallback_id = getattr(request, "id", None)
        task_name = getattr(current_task, "name", None)
    except Exception:  # noqa: BLE001, S110 — deliberate: current_task is a proxy; never trust it
        pass

    return fallback_id or None, task_name


def _coerce_current(value) -> int | None:
    """`current` is the one argument we cannot do without. Coerce ints-ish
    things (a float counter, a numpy int) and reject everything else quietly
    — a bad argument from user code degrades to "no progress shown"."""
    try:
        current = int(value)
    except Exception:  # noqa: BLE001 — int() can raise anything a custom __int__ raises
        return None
    if current < 0:
        # Negative progress is meaningless and would render as a broken bar.
        return None
    return current


def _coerce_total(value) -> int | None:
    """A total of None, 0, or garbage all mean the same thing to the UI:
    open-ended progress with no denominator. Never invent one."""
    if value is None:
        return None
    try:
        total = int(value)
    except Exception:  # noqa: BLE001 — same as _coerce_current: garbage means no total
        return None
    if total <= 0:
        return None
    return total


def _coerce_message(value) -> str | None:
    """None means "leave whatever message is already on the row" (see the
    docstring of `report_progress`). Empty string is the explicit clear."""
    if value is None:
        return None
    try:
        message = value if isinstance(value, str) else str(value)
    except Exception:  # noqa: BLE001 — a __str__ that raises must not fail the task
        return None
    return message[:MAX_MESSAGE_CHARS]


def report_progress(current, total=None, message: str | None = None) -> None:
    """Record progress for the currently running task. Never raises.

    Call it from inside your own task body::

        report_progress(i + 1, len(rows), f"row {i + 1}")

    `total` is optional — omit it for open-ended work ("processed 412 rows"),
    and the dashboard shows a count instead of a percentage.

    Throttled to one database write per `MIN_WRITE_INTERVAL_SECONDS` per run,
    so it is safe inside a tight loop. The one call that always writes,
    throttle or not, is the completing one (`total is not None and current >=
    total`) — the final "10000 / 10000" is the update users actually look for,
    and dropping it would leave every finished task stuck at 99%.

    **Messages are sticky.** Passing `message=None` (the default) leaves any
    message already on the row untouched, so `report_progress(i, n)` in a loop
    does not blank a label set by an earlier annotated call. That is a
    consequence of the storage layer skipping `None` values (see
    `decorator._upsert_task_run`), but it is also the behaviour we want: the
    common case is a caller who sets a phase label once and then just counts.
    To clear a message deliberately, pass an empty string — `""` is written
    through and renders as no message.

    Silently does nothing (with a one-off WARNING) if there is no running task
    to attribute progress to, or if this process never called
    `celery_manager.setup(config)`.
    """
    try:
        _report_progress(current, total, message)
    except Exception:  # deliberate: see rule 1 in the module docstring
        # _persist_run already swallows storage errors; this is the outer net
        # for everything else (a hostile __int__, a Celery proxy blowing up),
        # so that no phrasing of a user's arguments can fail their task.
        logger.exception(
            "celery-manager: report_progress() failed; the task's own outcome "
            "is unaffected and no progress was recorded."
        )


def _report_progress(current, total, message) -> None:
    # Imported inside the function, not at module scope: `decorator` imports
    # this module for the ContextVar above, so a top-level import here would
    # be circular. Same technique as `decorator._find_matching_override`,
    # which imports its model in-function for the same reason. Looking the
    # module up lazily also means tests can monkeypatch
    # `decorator._session_factory` and have it take effect here.
    from celery_manager import decorator

    task_id, task_name = _resolve_run()
    if not task_id:
        _warn_no_task_id_once()
        return

    if decorator._sessions() is None:
        _warn_unconfigured_once()
        return

    current = _coerce_current(current)
    if current is None:
        return
    total = _coerce_total(total)
    message = _coerce_message(message)

    # The completion write bypasses the throttle; nothing else does.
    is_complete = total is not None and current >= total
    if not is_complete and _throttled(task_id):
        return

    decorator._persist_run(
        task_id,
        # Only used if no row exists yet, which happens when an *undecorated*
        # task reports progress in a deployment with no events listener
        # running: `task_runs.task_name` and `.status` are NOT NULL, so a
        # progress-only insert without them fails the constraint and the
        # progress is silently dropped. "started" is honest — a task that is
        # reporting progress is by definition running — and because these are
        # create-only they can never restate a status over the decorator's or
        # the listener's.
        create_defaults={"task_name": task_name or "unknown", "status": "started"},
        progress_current=current,
        progress_total=total,
        progress_message=message,
        progress_updated_at=datetime.now(timezone.utc),
    )
    _last_write.set((task_id, time.monotonic()))


def _throttled(task_id: str) -> bool:
    """True if this run wrote too recently to write again.

    Monotonic clock deliberately: a wall-clock jump (NTP step, DST on a
    misconfigured host) must not either freeze progress for hours or defeat
    the throttle entirely.

    Note this is a *pre*-write check that never queues anything, so the last
    update of an open-ended (`total=None`) loop can be dropped and never
    re-sent — the row then shows a count a fraction of a second stale forever.
    Accepted: the run's terminal status is what tells the user it finished,
    and the alternative (a flush hook) is a lot of machinery for half a
    second of staleness.
    """
    previous = _last_write.get()
    if previous is None or previous[0] != task_id:
        return False
    return (time.monotonic() - previous[1]) < MIN_WRITE_INTERVAL_SECONDS


@contextlib.contextmanager
def _run_context(task_id: str):
    """Bind `task_id` as the run that `report_progress()` writes to, for the
    duration of one task body. Used by `@track`.

    Also clears the throttle bookkeeping on entry and restores it on exit, so
    a worker that reuses a thread (prefork's main thread, a thread/gevent
    pool's workers) can neither leak a task id into the next run nor let one
    run's write timestamp throttle another's first update.
    """
    id_token = _current_task_id.set(task_id)
    throttle_token = _last_write.set(None)
    try:
        yield
    finally:
        _current_task_id.reset(id_token)
        _last_write.reset(throttle_token)
