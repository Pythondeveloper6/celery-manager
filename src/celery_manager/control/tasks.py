from __future__ import annotations

import ast
import json
from datetime import datetime

from celery import Celery
from sqlalchemy import or_

from celery_manager.control.audit import record as record_audit
from celery_manager.db.models import TaskRun
from celery_manager.db.runs import lock_task_run

# Maximum rows a list endpoint will hand back in one response, and the ceiling
# each route clamps a caller-supplied `limit` to (`GET /api/tasks` and
# `GET /api/audit` both use it — one number, so the API has one answer to
# "how much can I ask for"). Lives here next to the query builder rather than
# in the routes so the control layer documents its own contract, but the
# clamping itself happens at the API boundary — a Python caller of
# `list_task_runs` is trusted and may legitimately ask for more.
MAX_PAGE_SIZE = 500


class TaskRunNotFound(ValueError):
    """No `task_runs` row exists for the given task id.

    Subclasses `ValueError` rather than `LookupError` purely so this stays a
    non-breaking change: `retry_task` has always raised a bare `ValueError`
    here, and any Python caller already catching that keeps working. The
    reason for having a type at all is the REST layer — it needs to tell
    "you asked about a task we've never heard of" (404) apart from "the row
    exists but we could not make sense of its stored args" (also a
    `ValueError`, but a 500-worthy one), and a message-string match is not a
    way to tell exceptions apart.
    """


def _decode_stored_call_argument(raw: str | None, field: str, task_id: str, empty):
    """Decode a stored `task_runs.args`/`.kwargs` back into real Python.

    Two writers, and historically two formats. The decorator writes
    `json.dumps(...)`; the events listener used to write `str(event["args"])`,
    and Celery's event payload is already a *repr* — `"(5,)"`, `"{'b': 2}"`.
    The listener now normalises those to JSON (`listener._normalise_call_argument`),
    but it keeps the repr when it cannot convert it, and rows written by
    older versions are still repr. So `json.loads` alone raised
    `JSONDecodeError` for every run the listener recorded and the decorator
    did not, i.e. every undecorated task, which surfaced as a 500 from the
    dashboard's retry button. Try JSON first (the format we write), then
    `ast.literal_eval` (the format we read back from Celery); the latter is
    the safe evaluator, never `eval`.

    Refuses rather than guesses when neither works, or when the decoded value
    is the wrong shape: re-dispatching somebody's task with silently dropped
    or reinterpreted arguments is a much worse outcome than an error message
    telling the operator to supply args explicitly. Celery's own event repr
    is truncated for long values, so "neither works" is a real case, not a
    theoretical one.
    """
    if not raw:
        return empty
    try:
        value = json.loads(raw)
    except ValueError:
        try:
            value = ast.literal_eval(raw)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            raise ValueError(
                f"could not decode the stored {field} for {task_id} ({raw!r}); "
                f"pass {field} explicitly to retry this run"
            ) from None
    if isinstance(empty, list) and isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, type(empty)):
        # ValueError is the contract callers (and tests) rely on for bad input.
        raise ValueError(  # noqa: TRY004
            f"the stored {field} for {task_id} decoded to "
            f"{type(value).__name__}, not {type(empty).__name__}; "
            f"pass {field} explicitly to retry this run"
        )
    return value


def _escape_like(term: str) -> str:
    """Neutralize LIKE wildcards in user-supplied search text.

    `%` and `_` are wildcards inside a LIKE pattern, so an operator searching
    their task kwargs for the literal string "100%" would otherwise match
    *every* row — a search box that silently returns everything looks like a
    broken filter, not like zero results. The escape character itself has to
    go first, or escaping `%` would re-escape the backslash we just added.

    Callers must pass `escape="\\\\"` alongside the returned pattern; the
    escape character is only meaningful to LIKE if it is declared.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_task_filters(
    query,
    *,
    status: str | None = None,
    task_name: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    """Apply the shared task-run filter chain to a query.

    Deliberately the *single* place the filters are expressed, used by both
    `list_task_runs` and `count_task_runs`. The classic bug in a paginated,
    filtered list is a total that quietly disagrees with the page it labels —
    "Showing 50 of 1,284" when the 1,284 was counted without the search term.
    Duplicating the chain in two functions is what lets them drift; sharing
    the builder makes drift structurally impossible, so keep it that way and
    add new filters only here.
    """
    if status is not None:
        query = query.filter_by(status=status)
    if task_name is not None:
        query = query.filter_by(task_name=task_name)
    if q:
        # args/kwargs are JSON stored as Text (see db/models.py), so this is a
        # pragmatic substring match over the serialized JSON, not a real JSON
        # query — searching "alice" finds a run whose kwargs contain it, but
        # there is no way to ask for `kwargs.user == "alice"`. That trade-off
        # was accepted for v1. `.ilike()` compiles to a case-insensitive LIKE
        # on SQLite, which has no native ILIKE.
        pattern = f"%{_escape_like(q)}%"
        query = query.filter(
            or_(
                TaskRun.task_name.ilike(pattern, escape="\\"),
                TaskRun.task_id.ilike(pattern, escape="\\"),
                TaskRun.args.ilike(pattern, escape="\\"),
                TaskRun.kwargs.ilike(pattern, escape="\\"),
            )
        )
    # Either date bound excludes rows with `started_at IS NULL` — SQL
    # comparisons against NULL are never true — and that is the intended
    # meaning: a queued run has not started, so it did not run "within this
    # range" and should not appear under a date filter.
    #
    # `date_to` is a plain `<=` against the instant, not a calendar day. A
    # caller who means "all of Aug 14" must pass an end-of-day `date_to`
    # (23:59:59.999999, or the start of Aug 15 if they prefer a half-open
    # range) — the frontend's date picker does exactly that. Truncating to a
    # day here would be wrong for the callers that legitimately want an hour
    # window.
    if date_from is not None:
        query = query.filter(TaskRun.started_at >= date_from)
    if date_to is not None:
        query = query.filter(TaskRun.started_at <= date_to)
    return query


def list_task_runs(
    session,
    *,
    status: str | None = None,
    task_name: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TaskRun]:
    """One page of task runs, newest first, under the given filters.

    Ordered by the surrogate `id` rather than `started_at`: `started_at` is
    nullable (a queued run has none), so ordering by it would scatter queued
    rows unpredictably, and `id` is monotonic in insertion order anyway.
    """
    query = _apply_task_filters(
        session.query(TaskRun),
        status=status, task_name=task_name, q=q, date_from=date_from, date_to=date_to,
    )
    return query.order_by(TaskRun.id.desc()).offset(offset).limit(limit).all()


def count_task_runs(
    session,
    *,
    status: str | None = None,
    task_name: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> int:
    """How many runs match these filters, ignoring limit/offset.

    Exists so the dashboard can honestly say "showing 50 of 1,284" and know
    whether a next page exists. Takes exactly the filter arguments
    `list_task_runs` takes, and routes them through the same
    `_apply_task_filters` — see that function for why sharing matters.
    """
    query = _apply_task_filters(
        session.query(TaskRun),
        status=status, task_name=task_name, q=q, date_from=date_from, date_to=date_to,
    )
    # count() over the filtered entity; SQLAlchemy wraps this in a
    # SELECT count(*) subquery, so no rows are loaded into memory.
    return query.count()


def list_retries_of(session, task_id: str) -> list[TaskRun]:
    """Every run dispatched as a retry of `task_id`, oldest first.

    More than one is normal: an operator can retry the same failed run
    repeatedly, and each attempt gets its own row pointing back here.
    Oldest-first because the detail view renders this as a chronological
    "retry history" list, unlike the newest-first main task list.
    """
    return session.query(TaskRun).filter_by(retried_from_id=task_id).order_by(TaskRun.id).all()


def get_task_run(session, task_id: str) -> TaskRun | None:
    return session.query(TaskRun).filter_by(task_id=task_id).one_or_none()


def start_task(
    celery_app: Celery,
    task_name: str,
    args: list,
    kwargs: dict,
    session=None,
    username: str | None = None,
) -> str:
    """Dispatch a task on demand (e.g. from the dashboard's 'run now' action).

    Pass `session` and `username` to record the operator action in the audit
    log, the way `retry_task` already does. Both are optional because
    `retry_task` calls this internally and records its own, more specific
    `retry` entry — auditing here unconditionally would double-log every
    retry as a retry *and* a start.
    """
    result = celery_app.send_task(task_name, args=args, kwargs=kwargs)
    if session is not None and username is not None:
        record_audit(
            session, username, "start", "task", result.id,
            {"task_name": task_name, "args": args, "kwargs": kwargs},
        )
    return result.id


def cancel_task(
    celery_app: Celery,
    task_id: str,
    terminate: bool = False,
    session=None,
    username: str | None = None,
) -> None:
    """Revoke a queued task, or terminate it if already running (terminate=True).

    Pass `session` and `username` to record the action in the audit log; see
    `start_task` above for why they're optional.
    """
    celery_app.control.revoke(task_id, terminate=terminate)
    if session is not None and username is not None:
        record_audit(session, username, "cancel", "task", task_id, {"terminate": terminate})


def retry_task(celery_app: Celery, session, task_id: str, username: str, args: list | None = None, kwargs: dict | None = None) -> str:
    """Re-run a task, by default with its original args/kwargs; pass
    args/kwargs to override them.

    Raises `TaskRunNotFound` if `task_id` names no stored run (the REST layer
    turns that into a 404), and a plain `ValueError` if the stored args or
    kwargs cannot be decoded — see `_decode_stored_call_argument` for why
    that is refused rather than guessed at.

    Also stamps the new dispatch's row with `retried_from_id`, so the retry
    chain is visible on the detail view immediately — before any Celery event
    for the new task_id has been received, which in the documented topology
    (a separate `celery-manager events` process) may be seconds away or, if
    the listener isn't running at all, never.
    """
    run = get_task_run(session, task_id)
    if run is None:
        raise TaskRunNotFound(f"no task run found for {task_id}")

    final_args = args if args is not None else _decode_stored_call_argument(run.args, "args", task_id, [])
    final_kwargs = kwargs if kwargs is not None else _decode_stored_call_argument(run.kwargs, "kwargs", task_id, {})

    new_id = start_task(celery_app, run.task_name, final_args, final_kwargs)

    # Upsert, never check-then-insert. `task_runs.task_id` is UNIQUE, and by
    # the time send_task() returns, two other writers may already be racing
    # us for this brand-new uuid: the events listener (task-sent /
    # task-received) and, if the worker is quick, the `@track` decorator's
    # `started` row. The previous version *checked* for an existing row and
    # then inserted, which only narrowed the window — a row landing between
    # the check and the commit still raised IntegrityError, so the dashboard's
    # retry button returned 500 for a retry that had in fact been dispatched,
    # and the lineage was lost. `lock_task_run` makes the insert itself
    # tolerate a concurrent one (see db/runs.py), which also covers
    # bulk_retry, whose next item used to fail on the poisoned session.
    #
    # Why not pre-assign the uuid and write the lineage row *before*
    # dispatching? It would close the window from the other side, but a
    # dispatch that then fails would leave a phantom `queued` run behind, and
    # it changes the send_task call every caller and test asserts on. The
    # upsert is sufficient on its own.
    #
    # Only `retried_from_id` is forced on an existing row. The listener owns
    # status/queue/worker and the decorator owns the outcome, and both know
    # more than we do; the seed values are create-only, so the dashboard has
    # something to show for the new run in the meantime without ever
    # restating `queued` over a run that has already started or finished.
    new_run = lock_task_run(session, new_id, seed={
        "task_name": run.task_name,
        "status": "queued",
        "args": json.dumps(final_args),
        "kwargs": json.dumps(final_kwargs),
    })
    new_run.retried_from_id = task_id

    # record_audit commits, which flushes the row above with it — but commit
    # explicitly rather than relying on that, so the lineage is durable even
    # if auditing is ever made optional here the way it is in start_task.
    session.commit()
    record_audit(session, username, "retry", "task", task_id, {"new_task_id": new_id})
    return new_id


def bulk_retry(celery_app: Celery, session, task_ids: list[str], username: str) -> list[str]:
    return [retry_task(celery_app, session, task_id, username) for task_id in task_ids]
