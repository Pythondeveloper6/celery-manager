"""Broker queue introspection: which queues exist, how deep they are, purge.

Two numbers per queue, and they are deliberately kept apart:

- **broker depth** — messages the broker is holding *right now*, read with a
  passive `queue_declare`. This is the truth about the backlog, but it is
  only as good as our view of the broker.
- **recorded queued** — `task_runs` rows in this queue whose status is still
  `queued`. That is what celery-manager has *seen* (task-sent/received
  events, retry seeds). It lags the broker, misses tasks sent while the
  listener was down, and keeps counting runs a worker has reserved for a
  future ETA. The two disagree routinely; neither is wrong, they answer
  different questions, so the API and the UI label them separately.

Every broker call runs on a small worker pool with a hard deadline. A broker
that accepts the TCP connection and then stops answering would otherwise pin
the HTTP request (and the dashboard's 5-second auto-refresh would pile more
on top of it). On timeout the request answers with `depth: null` and a reason;
the stuck call finishes — or dies — in the background, bounded by the pool.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone

from celery import Celery
from sqlalchemy import func

from celery_manager.control.audit import record as record_audit
from celery_manager.db.models import TaskRun

# Seconds to wait for the broker to accept a connection, and the overall
# budget for one listing/purge. Short on purpose: this backs an auto-refreshing
# page, and "the broker didn't answer in 3s" is itself the useful answer.
CONNECT_TIMEOUT = 2.0
BROKER_DEADLINE = 3.0

# `inspect().active_queues()` always waits the full timeout for workers that
# never reply, so it is kept short and its answer is cached: the set of queues
# workers consume rarely changes, and paying ~1s on every 5s refresh would make
# the page feel broken.
WORKER_INSPECT_TIMEOUT = 1.0
WORKER_QUEUES_TTL = 30.0

# Two threads: one for a listing, one for a purge. If both are wedged on a
# dead broker, further calls queue behind them and hit their own deadline —
# bounded, rather than a thread per request.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cm-broker")

_worker_cache_lock = threading.Lock()
_worker_cache: dict[int, tuple[float, set[str]]] = {}


class QueueBrokerError(RuntimeError):
    """The broker could not be asked (unreachable, timed out, unsupported)."""


class QueueNotFound(LookupError):
    """The broker says the queue does not exist (AMQP 404 on purge)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def configured_queue_names(celery_app: Celery) -> dict[str, set[str]]:
    """Queue names the app's configuration mentions, with where each came from.

    Sources: `task_default_queue` ("default"), `task_queues` ("config") and
    `task_routes` ("routes"). Routes may be a dict, a list of dicts, or
    router callables; only the declarative forms can be read without calling
    user code, so callables are skipped rather than guessed at.
    """
    conf = celery_app.conf
    found: dict[str, set[str]] = {}

    def add(name, source):
        if isinstance(name, str) and name:
            found.setdefault(name, set()).add(source)

    add(conf.task_default_queue or "celery", "default")

    queues = conf.task_queues
    if isinstance(queues, dict):
        for name in queues:
            add(name, "config")
    elif queues:
        for queue in queues:
            add(getattr(queue, "name", None), "config")

    routes = conf.task_routes
    route_maps = routes if isinstance(routes, (list, tuple)) else [routes]
    for route_map in route_maps:
        if not isinstance(route_map, dict):
            continue
        for target in route_map.values():
            # `{"pkg.task": "feeds"}` names the queue directly;
            # `{"pkg.task": {"queue": "feeds"}}` is the long form.
            if isinstance(target, str):
                add(target, "routes")
            elif isinstance(target, dict):
                queue = target.get("queue")
                add(getattr(queue, "name", queue), "routes")
    return found


def recorded_queued_counts(session) -> tuple[dict[str, int], int]:
    """`task_runs` rows still `queued`, per queue, plus those with no queue.

    Rows with no queue are real (decorator-written rows and retry seeds don't
    know the queue until the events listener fills it in), so they are counted
    separately rather than dropped or folded into the default queue.
    """
    rows = (
        session.query(TaskRun.queue, func.count(TaskRun.id))
        .filter(TaskRun.status == "queued")
        .group_by(TaskRun.queue)
        .all()
    )
    per_queue = {name: count for name, count in rows if name}
    unassigned = sum(count for name, count in rows if not name)
    return per_queue, unassigned


def recorded_queue_names(session) -> set[str]:
    rows = session.query(TaskRun.queue).filter(TaskRun.queue.isnot(None)).distinct().all()
    return {name for (name,) in rows if name}


def _open_connection(celery_app: Celery):
    conn = celery_app.connection_for_read(connect_timeout=CONNECT_TIMEOUT)
    # `connect()` is a single attempt (max_retries=1), not kombu's default
    # retry-forever ensure_connection.
    conn.connect()
    return conn


def _transport_name(conn) -> str:
    transport = getattr(conn, "transport", None)
    return str(getattr(transport, "driver_type", None) or getattr(conn, "transport_cls", "") or "unknown")


def _unsupported_reason(transport: str) -> str | None:
    """Why depth can't be read on this transport, or None if it can."""
    if transport == "memory":
        # The memory transport answers queue_declare happily — about *this*
        # process's private queues, which no worker ever reads. A confident 0
        # would be a lie, so say so instead.
        return "the in-memory broker is private to each process, so its queues can't be seen from here"
    return None


def _is_virtual(conn) -> bool:
    """Redis, SQS etc. go through kombu's virtual transport, which always
    reports 0 consumers — so for those, consumers is unknown, not zero."""
    try:
        from kombu.transport.virtual import Transport as VirtualTransport
    except ImportError:  # pragma: no cover - kombu always ships it
        return False
    return isinstance(getattr(conn, "transport", None), VirtualTransport)


def _is_not_found(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "reply_code", None)
    return str(code) == "404" or code == 404 or "NOT_FOUND" in str(exc)


def _declare_one(conn, name: str, virtual: bool) -> dict:
    # A fresh channel per queue: on AMQP a 404 from a passive declare closes
    # the channel, and every later queue would then fail on a dead channel.
    channel = conn.channel()
    try:
        ok = channel.queue_declare(queue=name, passive=True)
        return {
            "depth": int(ok.message_count),
            "consumers": None if virtual else int(ok.consumer_count),
            "exists": True,
            "reason": None,
        }
    except Exception as exc:  # noqa: BLE001 — every transport raises its own types; classified below
        if _is_not_found(exc):
            # Redis deletes a list key when it empties, so "not found" there
            # just means "empty" — not worth a note. On RabbitMQ it means
            # nobody has declared it yet; still nothing waiting in it, but an
            # operator expecting a consumer would want to know.
            return {
                "depth": 0,
                "consumers": None if virtual else 0,
                "exists": False,
                "reason": None if virtual else "not declared on the broker yet",
            }
        return {"depth": None, "consumers": None, "exists": None, "reason": f"broker refused: {exc}"}
    finally:
        try:
            channel.close()
        except Exception:  # noqa: BLE001, S110 — closing a dead channel is best-effort
            pass


def _worker_queue_names(celery_app: Celery, conn) -> set[str] | None:
    """Queues live workers consume from, cached; None if nobody answered."""
    key = id(celery_app)
    with _worker_cache_lock:
        cached = _worker_cache.get(key)
        if cached and time.monotonic() - cached[0] < WORKER_QUEUES_TTL:
            return cached[1]
    try:
        replies = celery_app.control.inspect(timeout=WORKER_INSPECT_TIMEOUT, connection=conn).active_queues() or {}
    except Exception:  # noqa: BLE001 — no worker answer is "unknown", never an error
        return None
    names = {q.get("name") for queues in replies.values() for q in (queues or []) if q.get("name")}
    with _worker_cache_lock:
        _worker_cache[key] = (time.monotonic(), names)
    return names


def _probe_broker(celery_app: Celery, names: list[str], include_workers: bool) -> dict:
    conn = _open_connection(celery_app)
    try:
        transport = _transport_name(conn)
        reason = _unsupported_reason(transport)
        if reason:
            return {"transport": transport, "unsupported": reason, "depths": {}, "worker_queues": None}
        worker_queues = _worker_queue_names(celery_app, conn) if include_workers else None
        all_names = sorted(set(names) | (worker_queues or set()))
        virtual = _is_virtual(conn)
        depths = {name: _declare_one(conn, name, virtual) for name in all_names}
        return {"transport": transport, "unsupported": None, "depths": depths, "worker_queues": worker_queues}
    finally:
        try:
            conn.release()
        except Exception:  # noqa: BLE001, S110 — releasing a broken connection is best-effort
            pass


def _run_with_deadline(fn, *args):
    future = _executor.submit(fn, *args)
    try:
        return future.result(timeout=BROKER_DEADLINE)
    except FutureTimeout:
        raise QueueBrokerError(f"the broker did not answer within {BROKER_DEADLINE:g}s") from None


def list_queues(celery_app: Celery, session, include_workers: bool = True) -> dict:
    """Every queue we know of, with broker depth and recorded-queued counts.

    Never raises for broker trouble: an unreachable, slow or unsupported
    broker yields `depth: null` per queue and a `broker.error` explaining why,
    because the recorded counts are still worth showing.
    """
    sources = configured_queue_names(celery_app)
    for name in recorded_queue_names(session):
        sources.setdefault(name, set()).add("runs")
    recorded, unassigned = recorded_queued_counts(session)
    for name in recorded:
        sources.setdefault(name, set()).add("runs")

    broker = {"transport": None, "error": None, "unsupported": False, "workers_replied": None}
    depths: dict[str, dict] = {}
    try:
        probe = _run_with_deadline(_probe_broker, celery_app, list(sources), include_workers)
    except QueueBrokerError as exc:
        broker["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — unreachable broker, auth failure, bad URL: report, don't fail
        broker["error"] = f"could not reach the broker: {exc}"
    else:
        broker["transport"] = probe["transport"]
        if probe["unsupported"]:
            broker["unsupported"] = True
            broker["error"] = probe["unsupported"]
        depths = probe["depths"]
        worker_queues = probe["worker_queues"]
        if worker_queues is not None:
            broker["workers_replied"] = bool(worker_queues)
            for name in worker_queues:
                sources.setdefault(name, set()).add("workers")

    default_queue = celery_app.conf.task_default_queue or "celery"
    queues = []
    for name in sorted(sources, key=lambda n: (n != default_queue, n)):
        depth = depths.get(name)
        queues.append({
            "name": name,
            "sources": sorted(sources[name]),
            "depth": depth["depth"] if depth else None,
            "consumers": depth["consumers"] if depth else None,
            "exists": depth["exists"] if depth else None,
            "reason": depth["reason"] if depth else broker["error"],
            "recorded_queued": recorded.get(name, 0),
        })
    return {"broker": broker, "queues": queues, "recorded_queued_unassigned": unassigned}


def _purge_on_broker(celery_app: Celery, name: str) -> int:
    conn = _open_connection(celery_app)
    try:
        reason = _unsupported_reason(_transport_name(conn))
        if reason:
            raise QueueBrokerError(reason)
        channel = conn.channel()
        try:
            removed = channel.queue_purge(name)
        except Exception as exc:
            if _is_not_found(exc):
                raise QueueNotFound(name) from None
            raise
        finally:
            try:
                channel.close()
            except Exception:  # noqa: BLE001, S110 — best-effort close
                pass
        return int(removed or 0)
    finally:
        try:
            conn.release()
        except Exception:  # noqa: BLE001, S110 — best-effort release
            pass


def purge_queue(celery_app: Celery, session, name: str, username: str) -> dict:
    """Drop every ready message in `name`; mark the runs that went with them.

    A purged message never reaches a worker, so no task-revoked event will
    ever arrive for it — left alone, its run would read `queued` forever.
    Hence: when the broker reports it removed at least one message, every
    `queued` run recorded in this queue *without an ETA* is marked `revoked`.

    The ETA exclusion is because workers pull ETA/countdown messages off the
    queue immediately and hold them until due; a purge does not touch those,
    so those runs will still execute. The rows marked are a best guess —
    the broker returns a count, not ids — which is why the audit row records
    both numbers, and why the marking is self-correcting: if a marked run
    does execute, its succeeded/failed event overwrites `revoked` (later
    terminal status wins in the listener).

    Nothing is marked when the broker removed nothing: then no run of ours
    can have been lost, and a stale `queued` row is some other problem.

    Raises `QueueNotFound` (AMQP 404) or `QueueBrokerError` (unreachable,
    timed out, unsupported transport); nothing is written in either case.
    """
    try:
        removed = _run_with_deadline(_purge_on_broker, celery_app, name)
    except (QueueNotFound, QueueBrokerError):
        raise
    except Exception as exc:  # refused, auth failure, bad URL: one broker error type
        raise QueueBrokerError(f"could not reach the broker: {exc}") from exc

    marked: list[str] = []
    if removed > 0:
        runs = (
            session.query(TaskRun)
            .filter(TaskRun.status == "queued", TaskRun.queue == name, TaskRun.eta.is_(None))
            .all()
        )
        now = _now()
        for run in runs:
            run.status = "revoked"
            run.finished_at = now
            marked.append(run.task_id)
        session.commit()

    record_audit(session, username, "purge", "queue", name, {
        "messages_removed": removed,
        "runs_marked_revoked": len(marked),
        "task_ids": marked,
    })
    return {"queue": name, "messages_removed": removed, "runs_marked_revoked": len(marked)}
