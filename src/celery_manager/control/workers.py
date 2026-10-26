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

from celery import Celery

from celery_manager.control.audit import record as record_audit

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
