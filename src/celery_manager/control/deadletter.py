"""The dead-letter view: runs that failed for good, and re-running them.

Nothing here is stored. "Dead letter" is a *query* over `task_runs`, so there
is no extra table to keep in step with the runs and no migration:

    a run is a dead letter when its status is `failed` and nothing was
    dispatched from it afterwards (no row has `retried_from_id` = its id).

Each half of that rule is deliberate:

- `failed`, not `retried`: when Celery retries a task it reuses the task id,
  and the row reads `retried` until the last attempt. A run is only `failed`
  once Celery has given up, which is exactly "used up all its retries" (or was
  never configured to retry).
- No later run points back at it: `retry_task` and `rerun_with_arguments` both
  stamp the new run's `retried_from_id`, so the moment an operator re-runs a
  dead letter it leaves the list, whether the re-run succeeds, is still
  queued, or fails again. A failed re-run is itself the end of its own chain,
  so it shows up as a fresh dead letter, with its earlier attempts counted.

There is no "dismiss": hiding a dead letter without re-running it would need
somewhere to remember the decision, and the only place that needs no
migration is the audit log, which is not something to query on every page
load. It is left out until there is a column for it.
"""

from __future__ import annotations

from datetime import datetime

from celery import Celery
from sqlalchemy import DateTime, exists, func
from sqlalchemy.orm import aliased

from celery_manager.control.analytics import split_exception
from celery_manager.control.audit import record as record_audit
from celery_manager.control.tasks import (
    TaskRunNotFound,
    _decode_stored_call_argument,
    start_task,
)
from celery_manager.db.models import FailureResolution, TaskRun
from celery_manager.db.runs import lock_task_run

# Most runs one re-run request may touch. Over it the request is refused, not
# truncated, for the same reason bulk cancel is: re-running an arbitrary
# first-N of a bigger selection is not what the operator asked for.
MAX_BULK_RERUN = 500

# Most dead letters read to build the exception-type summary or to apply the
# exception filter. Exception *type* is parsed from the stored text in Python
# (`split_exception`), the same way the Insights failure groups do it, because
# SQL cannot do that portably. The scan is newest first; when it is cut short
# the result says so.
MAX_SCAN_ROWS = 20_000

# Longest retry chain walked back from one dead letter when counting its
# earlier attempts. A guard against a lineage cycle in hand-edited data, not a
# limit anyone will meet.
MAX_CHAIN_DEPTH = 50


class NotDeadLetter(ValueError):
    """One or more of the given runs is not a dead letter (it did not fail,
    or something was already re-run from it). Nothing was dispatched."""

    def __init__(self, task_ids: list[str]):
        self.task_ids = task_ids
        listed = ", ".join(task_ids[:5]) + (f" and {len(task_ids) - 5} more" if len(task_ids) > 5 else "")
        super().__init__(
            f"not a dead letter (it has not failed, or was already run again): {listed}. "
            "Reload the list and try again."
        )


class BulkRerunTooLarge(ValueError):
    """The selection exceeds MAX_BULK_RERUN; nothing was dispatched."""

    def __init__(self, count: int):
        self.count = count
        super().__init__(
            f"{count} runs selected; re-running is limited to {MAX_BULK_RERUN} at a time. "
            "Select fewer runs and try again."
        )


class MixedTaskNames(ValueError):
    """Edited arguments were given for runs of more than one task. Arguments
    that suit one task mean nothing to another, so this is refused."""

    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(
            "arguments can only be edited for runs of a single task, but the selection has "
            f"{len(names)} ({', '.join(names[:5])}). Select one task, or re-run with the original arguments."
        )


def _dead_letter_query(session, *, task_name: str | None = None, since: datetime | None = None):
    # NOT EXISTS over the retry lineage. `retried_from_id` is indexed, so this
    # is an index probe per failed row rather than a scan.
    later = aliased(TaskRun)
    query = session.query(TaskRun).filter(
        TaskRun.status == "failed",
        ~exists().where((FailureResolution.task_id == TaskRun.task_id) & FailureResolution.state.in_(("acknowledged", "ignored"))).correlate(TaskRun),
        ~exists().where(later.retried_from_id == TaskRun.task_id).correlate(TaskRun),
    )
    if task_name is not None:
        query = query.filter(TaskRun.task_name == task_name)
    if since is not None:
        # When it died: finished_at, or started_at for rows a listener wrote
        # without ever seeing a finish. NULL on both never matches a bound.
        failed_at = func.coalesce(TaskRun.finished_at, TaskRun.started_at, type_=DateTime(timezone=True))
        query = query.filter(failed_at >= since)
    return query


def exception_type(run: TaskRun) -> str:
    return split_exception(run.exception)[0]


def list_dead_letters(
    session,
    *,
    task_name: str | None = None,
    exception: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TaskRun], int]:
    """One page of dead letters, newest first, and the total across all pages.

    `exception` is an exception *type* (`ConnectionError`), compared with the
    same parser the summary groups use, so a type picked from the summary
    always finds its runs. Without it the page and total come straight from
    SQL; with it the candidates are scanned (newest first, up to
    `MAX_SCAN_ROWS`) and filtered in Python.
    """
    query = _dead_letter_query(session, task_name=task_name, since=since)
    if exception is None:
        total = query.count()
        return query.order_by(TaskRun.id.desc()).offset(offset).limit(limit).all(), total

    candidates = (
        query.with_entities(TaskRun.id, TaskRun.exception)
        .order_by(TaskRun.id.desc())
        .limit(MAX_SCAN_ROWS)
        .all()
    )
    ids = [row_id for row_id, text in candidates if split_exception(text)[0] == exception]
    page_ids = ids[offset:offset + limit]
    if not page_ids:
        return [], len(ids)
    rows = {r.id: r for r in session.query(TaskRun).filter(TaskRun.id.in_(page_ids)).all()}
    return [rows[i] for i in page_ids if i in rows], len(ids)


def summarize_dead_letters(session, *, since: datetime | None = None) -> dict:
    """Dead letters counted by task and exception type, biggest group first.

    Deliberately ignores the task and exception filters of the list: it is
    the menu those filters are picked from, and a menu that shrinks to the
    one thing already chosen cannot be used to pick another.
    """
    query = _dead_letter_query(session, since=since)
    rows = (
        query.with_entities(TaskRun.task_name, TaskRun.exception)
        .order_by(TaskRun.id.desc())
        .limit(MAX_SCAN_ROWS + 1)
        .all()
    )
    truncated = len(rows) > MAX_SCAN_ROWS
    groups: dict[tuple[str, str], dict] = {}
    for name, text in rows[:MAX_SCAN_ROWS]:
        key = (name, split_exception(text)[0])
        group = groups.setdefault(key, {"task_name": name, "exception_type": key[1], "count": 0})
        group["count"] += 1
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["task_name"], g["exception_type"]))
    return {"groups": ordered, "total": min(len(rows), MAX_SCAN_ROWS), "truncated": truncated}


def earlier_attempts(session, runs: list[TaskRun]) -> dict[str, int]:
    """For each run, how many runs came before it in its retry chain.

    Celery's own retries reuse the task id and are not recorded as separate
    rows, so this counts the *re-runs* made through celery-manager (the retry
    button, or this feature) that led to this failure. A first-time failure
    is 0. Walks one level of the lineage per query for the whole page, not
    one query per run.
    """
    depth = {r.task_id: 0 for r in runs}
    frontier = {r.task_id: r.retried_from_id for r in runs if r.retried_from_id}
    for _ in range(MAX_CHAIN_DEPTH):
        if not frontier:
            break
        parent_ids = set(frontier.values())
        parents = dict(
            session.query(TaskRun.task_id, TaskRun.retried_from_id).filter(TaskRun.task_id.in_(parent_ids)).all()
        )
        following: dict[str, str] = {}
        for origin, parent in frontier.items():
            # Counted even when the parent row was purged: the retry happened.
            depth[origin] += 1
            grandparent = parents.get(parent)
            if grandparent:
                following[origin] = grandparent
        frontier = following
    return depth


def last_traceback_line(run: TaskRun) -> str | None:
    """The line that names the error: the last non-empty line of the
    traceback, or the stored exception text when there is no traceback."""
    for source in (run.traceback, run.exception):
        lines = [line.strip() for line in (source or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return None


def _stored_unless_replaced(raw, field, task_id, empty, override):
    """The decoded stored argument, or None when it cannot be decoded but is
    being replaced anyway. A stored value that cannot be read only matters
    when it is the one being kept; replacing it is the way out of that error.
    "Edited" then means different from what could be read back."""
    try:
        return _decode_stored_call_argument(raw, field, task_id, empty)
    except ValueError:
        if override is None:
            raise
        return None


def rerun_with_arguments(
    celery_app: Celery,
    session,
    task_ids: list[str],
    args: list | None,
    kwargs: dict | None,
    username: str,
) -> dict:
    """Dispatch a new run for each dead letter, optionally with new arguments.

    `args`/`kwargs` of None keep each run's stored value; given, they replace
    it for every selected run. Edited arguments are only accepted when the
    selection is of one task (`MixedTaskNames`), since they were written for
    one signature.

    Everything is checked before anything is sent: unknown ids
    (`TaskRunNotFound`), runs that are not dead letters (`NotDeadLetter`),
    too many (`BulkRerunTooLarge`), the wrong argument shapes or stored
    arguments that cannot be decoded (`ValueError`). So a refused request
    changes nothing. A broker error part-way is different — the sends already
    made cannot be unmade — and is reported per run in `errors`.

    Each new run records `retried_from_id` (which is what removes the original
    from the dead-letter list) and goes to the original's queue. One audit row
    per original, saying whether the arguments differed from what was stored.
    """
    if args is not None and not isinstance(args, list):
        raise ValueError("args must be a JSON array")
    if kwargs is not None and not isinstance(kwargs, dict):
        raise ValueError("kwargs must be a JSON object")
    ids = list(dict.fromkeys(task_ids))
    if not ids:
        raise ValueError("select at least one run")
    if len(ids) > MAX_BULK_RERUN:
        raise BulkRerunTooLarge(len(ids))

    runs = {r.task_id: r for r in session.query(TaskRun).filter(TaskRun.task_id.in_(ids)).all()}
    missing = [i for i in ids if i not in runs]
    if missing:
        raise TaskRunNotFound(f"no task run found for {', '.join(missing[:5])}")

    # Which of the selected ones still are dead letters, in one query.
    still_dead = {
        task_id
        for (task_id,) in _dead_letter_query(session).with_entities(TaskRun.task_id).filter(TaskRun.task_id.in_(ids))
    }
    stale = [i for i in ids if i not in still_dead]
    if stale:
        raise NotDeadLetter(stale)

    names = sorted({r.task_name for r in runs.values()})
    if (args is not None or kwargs is not None) and len(names) > 1:
        raise MixedTaskNames(names)

    plan = []
    for task_id in ids:
        run = runs[task_id]
        # A stored value that cannot be decoded only matters when it is the
        # one being kept; replacing it is the way out of that error. "Edited"
        # then means different from what could be read back.
        stored_args = _stored_unless_replaced(run.args, "args", task_id, [], args)
        stored_kwargs = _stored_unless_replaced(run.kwargs, "kwargs", task_id, {}, kwargs)
        final_args = stored_args if args is None else args
        final_kwargs = stored_kwargs if kwargs is None else kwargs
        edited = final_args != stored_args or final_kwargs != stored_kwargs
        from celery_manager.control.policy import validate_dispatch
        validate_dispatch(session.info.get("cm_config"), run.task_name, final_args, final_kwargs,
                          edited=edited, session=session, principal=session.info.get("cm_principal"))
        plan.append((run, final_args, final_kwargs, edited))

    reruns: list[dict] = []
    errors: list[dict] = []
    for run, final_args, final_kwargs, edited in plan:
        try:
            # session= seeds the new run's row (queued, with its arguments) so
            # its page can be opened at once; no username, because the audit
            # row written below is the specific one for this action.
            new_id = start_task(
                celery_app, run.task_name, final_args, final_kwargs, session=session, queue=run.queue, _preserve_queue=True,
            )
        except Exception as exc:  # noqa: BLE001 - a broker failure on one run must not hide the others
            errors.append({"task_id": run.task_id, "error": str(exc) or type(exc).__name__})
            continue
        # `start_task` seeded the row; the listener may have beaten us to it.
        # Either way the lineage is forced on, which is what takes the
        # original off the dead-letter list.
        lock_task_run(session, new_id, seed={"task_name": run.task_name, "status": "queued"}).retried_from_id = (
            run.task_id
        )
        session.commit()
        details: dict = {"new_task_id": new_id, "args_edited": edited, "bulk": len(plan) > 1}
        if edited:
            details.update({"args": final_args, "kwargs": final_kwargs})
        record_audit(session, username, "rerun_dead_letter", "task", run.task_id, details)
        reruns.append({"task_id": run.task_id, "new_task_id": new_id, "args_edited": edited})
    return {"reruns": reruns, "errors": errors}
