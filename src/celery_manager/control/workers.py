"""Live worker introspection and remote control over Celery's broadcast API.

Nothing here touches celery-manager's own database except the audit log:
worker state lives in the workers themselves and is only ever as fresh as the
last broadcast. Every call takes a short `timeout` (the config's
`inspect_timeout_seconds`) because the HTTP request that triggered it is
blocked until the replies are in.

Broker failures are turned into `WorkerControlError` with a readable message
so the REST layer can answer 503 instead of a 500 with a stack trace — "the
broker is unreachable" is an operational state the dashboard must show, not a
bug.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from celery import Celery

from celery_manager.control.audit import record as record_audit
from celery_manager.control.masking import mask_call
from celery_manager.control.tasks import (
    _REVOKE_CHUNK,
    MAX_BULK_CANCEL,
    BulkCancelTooLarge,
)
from celery_manager.db.models import TaskRun

logger = logging.getLogger(__name__)

# What `Task.rate_limit` / `control.rate_limit` accept: a number of tasks,
# optionally per second/minute/hour ("10", "10/s", "2.5/m", "100/h"). A bare
# number means per second. The empty string clears the limit. Celery itself
# parses this on the *worker*, so a typo would otherwise only surface as an
# error reply (or, worse, an exception in the worker's control thread).
_RATE_LIMIT_RE = re.compile(r"^\d+(\.\d+)?(/[smh])?$")

# Hostnames are `name@host`; Celery imposes no format, but nothing legitimate
# is longer than this, and a cap keeps an audit row from carrying megabytes.
MAX_NAME_LENGTH = 255


class WorkerControlError(RuntimeError):
    """The broadcast could not be sent or its replies could not be read —
    almost always the broker being unreachable. The message is meant for an
    operator, not a developer."""


def is_valid_rate_limit(value: str) -> bool:
    return value == "" or bool(_RATE_LIMIT_RE.match(value))


def _broker_error(exc: Exception) -> WorkerControlError:
    logger.warning("celery-manager: worker broadcast failed: %r", exc)
    return WorkerControlError(
        f"Couldn't reach the Celery broker ({type(exc).__name__}: {exc}). "
        "Check that the broker is running and that celery-manager is configured "
        "with the same broker URL as your workers."
    )


def _short_pool(implementation: str | None) -> str | None:
    """`celery.concurrency.prefork:TaskPool` -> `prefork`."""
    if not implementation:
        return None
    module = implementation.split(":", 1)[0]
    return module.rsplit(".", 1)[-1]


def _task_ref(request: dict) -> dict:
    """The bits of a worker's task request the dashboard links and labels
    with. `scheduled()` wraps the request one level deeper; callers unwrap."""
    return {
        "id": request.get("id"),
        "name": request.get("name") or request.get("type"),
        "time_start": request.get("time_start"),
    }


def inspect_workers(celery_app: Celery, timeout: float) -> list[dict]:
    """One snapshot of every worker that answers within `timeout`.

    `ping` goes first with no reply limit, so it costs the full timeout — it
    is the only way to learn who is out there. The five follow-up calls are
    then addressed to exactly those workers with `limit=len(workers)`, so
    each returns as soon as all of them have replied instead of also waiting
    out the timeout. With no workers the whole call costs one timeout; with
    healthy workers, roughly one timeout plus a few round trips.

    A worker that answered ping but not a follow-up still appears, with the
    missing fields null/empty, rather than vanishing from the list.
    """
    try:
        pings = celery_app.control.inspect(timeout=timeout).ping() or {}
        names = sorted(pings)
        if not names:
            return []
        follow_up = celery_app.control.inspect(destination=names, timeout=timeout, limit=len(names))
        stats = follow_up.stats() or {}
        active = follow_up.active() or {}
        reserved = follow_up.reserved() or {}
        scheduled = follow_up.scheduled() or {}
        queues = follow_up.active_queues() or {}
    except Exception as exc:  # broker errors come in many types (kombu, socket, redis, amqp)
        raise _broker_error(exc) from exc

    workers = []
    for name in sorted(set(names) | set(stats)):
        st = stats.get(name) or {}
        pool = st.get("pool") or {}
        processed = st.get("total") or {}
        active_tasks = [_task_ref(r) for r in active.get(name) or []]
        reserved_tasks = [_task_ref(r) for r in reserved.get(name) or []]
        scheduled_tasks = [
            {**_task_ref(entry.get("request") or {}), "eta": entry.get("eta")}
            for entry in scheduled.get(name) or []
        ]
        workers.append({
            "name": name,
            "online": name in pings,
            "pid": st.get("pid"),
            "uptime_seconds": st.get("uptime"),
            "pool": _short_pool(pool.get("implementation")),
            # "max-concurrency" is the configured -c; "processes" lists the
            # live child pids, which is what pool_grow/pool_shrink change.
            "concurrency": pool.get("max-concurrency"),
            "pool_processes": len(pool["processes"]) if isinstance(pool.get("processes"), list) else None,
            "processed_total": sum(v for v in processed.values() if isinstance(v, int)),
            "processed_by_task": processed,
            "active_count": len(active_tasks),
            "reserved_count": len(reserved_tasks),
            "scheduled_count": len(scheduled_tasks),
            "active": active_tasks,
            "reserved": reserved_tasks,
            "scheduled": scheduled_tasks,
            "queues": sorted(q.get("name") for q in queues.get(name) or [] if q.get("name")),
        })
    return workers


def _summarise_replies(replies) -> dict:
    """Celery returns `[{hostname: {"ok": msg}}, {hostname: {"error": msg}}]`.
    Flatten that into who acknowledged and who refused (with why)."""
    acknowledged: list[str] = []
    errors: dict[str, str] = {}
    for reply in replies or []:
        if not isinstance(reply, dict):
            continue
        for host, body in reply.items():
            if isinstance(body, dict) and "error" in body:
                errors[host] = str(body["error"])
            else:
                acknowledged.append(host)
    return {"acknowledged": sorted(acknowledged), "errors": errors}


def _broadcast(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise _broker_error(exc) from exc


def shutdown_worker(celery_app: Celery, session, username: str, worker: str) -> dict:
    """Warm shutdown: the worker stops taking work, finishes what it is
    running, then exits. Celery sends no reply for shutdown, so there is no
    acknowledgement to report — the worker drops off the next refresh."""
    _broadcast(celery_app.control.shutdown, destination=[worker])
    record_audit(session, username, "worker_shutdown", "worker", worker)
    return {"ok": True}


def resize_pool(celery_app: Celery, session, username: str, worker: str, delta: int, timeout: float) -> dict:
    """Grow (`delta > 0`) or shrink (`delta < 0`) the worker's pool by
    `abs(delta)` processes. Only pools that support resizing (prefork) do
    anything; others answer with an error reply, which is passed through."""
    if delta == 0:
        raise ValueError("delta must not be zero")
    fn = celery_app.control.pool_grow if delta > 0 else celery_app.control.pool_shrink
    replies = _broadcast(fn, abs(delta), destination=[worker], reply=True, timeout=timeout, limit=1)
    result = _summarise_replies(replies)
    action = "worker_pool_grow" if delta > 0 else "worker_pool_shrink"
    record_audit(session, username, action, "worker", worker, {"n": abs(delta), **result})
    return result


def set_consumer(celery_app: Celery, session, username: str, worker: str, queue: str, consume: bool, timeout: float) -> dict:
    """Start (`consume=True`) or stop consuming from `queue` on one worker.
    Like every remote control command this lasts until the worker restarts;
    its `-Q` option wins again after that."""
    if consume:
        replies = _broadcast(
            celery_app.control.add_consumer, queue,
            destination=[worker], reply=True, timeout=timeout, limit=1,
        )
    else:
        replies = _broadcast(
            celery_app.control.cancel_consumer, queue,
            destination=[worker], reply=True, timeout=timeout, limit=1,
        )
    result = _summarise_replies(replies)
    action = "worker_add_consumer" if consume else "worker_cancel_consumer"
    record_audit(session, username, action, "worker", worker, {"queue": queue, **result})
    return result


def set_rate_limit(
    celery_app: Celery,
    session,
    username: str,
    task_name: str,
    rate_limit: str,
    timeout: float,
    destination: list[str] | None = None,
) -> dict:
    """Change `task_name`'s rate limit on running workers (all of them, or
    just `destination`). `""` removes the limit.

    Not persisted anywhere: each worker holds it in memory and falls back to
    the task's declared `rate_limit` when it restarts. Workers that start
    after this call never hear about it.
    """
    if not is_valid_rate_limit(rate_limit):
        raise ValueError(
            f"{rate_limit!r} is not a rate limit. Use a number of tasks per second, "
            "minute or hour such as 10/s, 10/m or 100/h, or leave it empty to remove the limit."
        )
    kwargs: dict = {"reply": True, "timeout": timeout}
    if destination:
        kwargs["destination"] = destination
        kwargs["limit"] = len(destination)
    replies = _broadcast(celery_app.control.rate_limit, task_name, rate_limit, **kwargs)
    result = _summarise_replies(replies)
    record_audit(
        session, username, "rate_limit", "task", task_name,
        {"rate_limit": rate_limit, "destination": destination, **result},
    )
    return result


def list_task_rate_limits(celery_app: Celery) -> list[dict]:
    """Every task registered on `celery_app` (Celery's own `celery.*`
    housekeeping tasks excluded) with the rate limit it declares in code.

    This is the *default* the worker starts with, read from this process's
    registry — not what a worker is currently enforcing after a runtime
    change; Celery has no inspect call that reports that.
    """
    rows = []
    for name, task in sorted(celery_app.tasks.items()):
        if name.startswith("celery."):
            continue
        limit = getattr(task, "rate_limit", None)
        rows.append({"task_name": name, "rate_limit": str(limit) if limit not in (None, "") else None})
    return rows


# --- live task index (shared with the stuck-task view) ---------------------


def live_task_index(celery_app: Celery, timeout: float) -> tuple[list[str], dict[str, dict]]:
    """Which live worker holds which task id, and in what state.

    Returns `(workers_that_replied, {task_id: {"worker", "state"}})` where
    state is `active` (executing now), `reserved` (prefetched, waiting for a
    pool slot) or `scheduled` (held until its ETA). Same ping-then-addressed
    follow-ups as `inspect_workers`, so it costs about one `timeout`.

    If two replies disagree about one id (a redelivered message landing on a
    second worker while the first still runs it), `active` wins, because
    "something is running this" is the fact the callers act on.
    """
    try:
        pings = celery_app.control.inspect(timeout=timeout).ping() or {}
        names = sorted(pings)
        if not names:
            return [], {}
        follow_up = celery_app.control.inspect(destination=names, timeout=timeout, limit=len(names))
        scheduled = follow_up.scheduled() or {}
        reserved = follow_up.reserved() or {}
        active = follow_up.active() or {}
    except Exception as exc:  # broker errors come in many types
        raise _broker_error(exc) from exc

    index: dict[str, dict] = {}
    # Least to most decisive, so a later state overwrites an earlier one.
    for state, replies in (("scheduled", scheduled), ("reserved", reserved), ("active", active)):
        for worker, items in replies.items():
            for item in items or []:
                request = (item.get("request") or {}) if state == "scheduled" else item
                task_id = request.get("id") if isinstance(request, dict) else None
                if task_id:
                    index[task_id] = {"worker": worker, "state": state}
    return names, index


# --- delayed (ETA / countdown) tasks ---------------------------------------

# Runs `list_delayed_tasks` reports as "recorded with a future ETA but held by
# no worker". A cap, not a page: the list exists to make the operator look,
# and past a hundred the count says as much as the rows would.
MAX_UNHELD_LISTED = 100


def _iso_utc(value) -> str | None:
    """A worker's ETA (ISO 8601, in whatever zone the worker's app uses) as
    UTC ISO 8601, so the dashboard compares like with like and a string sort
    is a time sort. A naive value means UTC, this codebase's convention;
    anything unparseable is passed through rather than dropped."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def list_delayed_tasks(celery_app: Celery, session, timeout: float, reveal_args: bool = False) -> dict:
    """Every ETA/countdown task live workers are holding, soonest ETA first,
    merged with what `task_runs` knows about each.

    A countdown or ETA task does not wait on the broker: the first worker to
    see it takes it off the queue at once and keeps it in memory until it is
    due. So the only place these are visible is the workers' own
    `inspect().scheduled()` — a worker that doesn't answer in time holds
    tasks this list can't show.

    Arguments are masked (`control.masking`) unless `reveal_args` is set;
    the REST layer only sets it for operators.

    `unheld_runs` lists runs recorded as `queued` with an ETA still in the
    future that no replying worker holds: still on the queue (nothing
    consumes it yet), held by a worker that didn't answer, or lost.

    Raises `WorkerControlError` if the broker can't be reached.
    """
    try:
        pings = celery_app.control.inspect(timeout=timeout).ping() or {}
        names = sorted(pings)
        scheduled = {}
        if names:
            follow_up = celery_app.control.inspect(destination=names, timeout=timeout, limit=len(names))
            scheduled = follow_up.scheduled() or {}
    except Exception as exc:  # broker errors come in many types
        raise _broker_error(exc) from exc

    tasks: list[dict] = []
    for worker in sorted(scheduled):
        for entry in scheduled.get(worker) or []:
            request = entry.get("request") or {}
            task_id = request.get("id")
            if not task_id:
                continue
            delivery = request.get("delivery_info") or {}
            args, kwargs = request.get("args"), request.get("kwargs")
            priority = entry.get("priority")
            tasks.append({
                "task_id": task_id,
                "task_name": request.get("name") or request.get("type"),
                "eta": _iso_utc(entry.get("eta")),
                "worker": worker,
                "queue": delivery.get("routing_key"),
                "priority": priority if priority is not None else delivery.get("priority"),
                "args_masked": mask_call(args, kwargs),
                "args": args if reveal_args else None,
                "kwargs": kwargs if reveal_args else None,
                "run": None,
            })

    ids = [t["task_id"] for t in tasks]
    runs = {r.task_id: r for r in session.query(TaskRun).filter(TaskRun.task_id.in_(ids)).all()} if ids else {}
    for task in tasks:
        run = runs.get(task["task_id"])
        if run is None:
            continue
        task["run"] = {"status": run.status, "schedule_name": run.schedule_name}
        task["queue"] = task["queue"] or run.queue
        task["task_name"] = task["task_name"] or run.task_name
    # A missing ETA (shouldn't happen for a scheduled entry) sorts last.
    tasks.sort(key=lambda t: (t["eta"] is None, t["eta"] or "", t["task_id"]))

    held = set(ids)
    unheld_query = (
        session.query(TaskRun)
        .filter(TaskRun.status == "queued", TaskRun.eta.isnot(None), TaskRun.eta > datetime.now(timezone.utc))
        .order_by(TaskRun.eta)
    )
    unheld = []
    truncated = False
    # Over-fetch by the held count so the ones filtered out below can't
    # starve the page; one row more than the cap says "there are more".
    for run in unheld_query.limit(MAX_UNHELD_LISTED + len(held) + 1).all():
        if run.task_id in held:
            continue
        if len(unheld) == MAX_UNHELD_LISTED:
            truncated = True
            break
        eta = run.eta if run.eta.tzinfo else run.eta.replace(tzinfo=timezone.utc)
        unheld.append({"task_id": run.task_id, "task_name": run.task_name, "eta": eta.isoformat(), "queue": run.queue})

    return {"tasks": tasks, "workers_replied": names, "unheld_runs": unheld, "unheld_truncated": truncated}


def cancel_delayed_tasks(celery_app: Celery, session, username: str, task_ids: list[str]) -> dict:
    """Revoke delayed tasks and mark their runs `revoked` straight away.

    The same `control.revoke` broadcast `cancel_task`/`bulk_cancel` send,
    with one difference that is the reason this exists: a worker holding a
    revoked ETA task only drops it — and only then emits `task-revoked` —
    when the ETA arrives. Left to the events listener, a task cancelled
    today with a countdown of a week would read `queued` for a week. So the
    run is marked here, the way `purge_queue` marks what it removed. If the
    revoke somehow doesn't take and the task runs anyway, its outcome event
    overwrites `revoked` (later terminal status wins in the listener).

    Ids with no recorded run are still revoked (a delayed task sent while
    the listener was down is exactly what this view surfaces); only runs
    still `queued`/`retried` are marked, never a finished one.

    Revokes live in each worker's memory: a worker restarted before the ETA
    forgets them and runs the task, unless workers run with `--statedb`.

    Raises `ValueError` for an empty selection, `BulkCancelTooLarge` above
    `MAX_BULK_CANCEL` ids and `WorkerControlError` if the broadcast fails —
    nothing is marked or audited in any of those cases.
    """
    ids = list(dict.fromkeys(t for t in task_ids if t))  # de-dupe, keep order
    if not ids:
        raise ValueError("send at least one task id")
    if len(ids) > MAX_BULK_CANCEL:
        raise BulkCancelTooLarge(len(ids))
    for start in range(0, len(ids), _REVOKE_CHUNK):
        _broadcast(celery_app.control.revoke, ids[start:start + _REVOKE_CHUNK])

    now = datetime.now(timezone.utc)
    marked = []
    for run in session.query(TaskRun).filter(TaskRun.task_id.in_(ids)).all():
        if run.status in ("queued", "retried"):
            run.status = "revoked"
            run.finished_at = now
            marked.append(run.task_id)
    session.commit()

    # The same audit actions as the Tasks page's cancel and bulk cancel, so
    # "who cancelled what" stays one filter in the audit log; `delayed` says
    # where it came from.
    if len(ids) == 1:
        record_audit(session, username, "cancel", "task", ids[0], {
            "terminate": False, "delayed": True, "run_marked_revoked": bool(marked),
        })
    else:
        record_audit(session, username, "cancel_bulk", "task", "delayed", {
            "terminate": False, "delayed": True, "revoked": len(ids),
            "runs_marked_revoked": len(marked), "task_ids": ids,
        })
    return {"revoked": len(ids), "runs_marked_revoked": len(marked)}
