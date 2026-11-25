"""Repair `task_runs` after the events listener was down.

Celery events are not stored: one sent while nothing listened is gone, so a
run can stay `queued` or `started` for ever. Three passes close the gap:

1. Result backend: a run that has been open for `reconcile_after_seconds` is
   looked up with `backend.get_task_meta`; a terminal answer is applied and the
   row is marked `status_source = "backend"`.
2. Workers and broker: a still-`started` run that every expected worker
   denies holding (active, reserved, scheduled) and that the Redis unacked set
   does not hold is marked `lost`. If any source is unknown, nothing changes.
   A later lifecycle event overwrites `lost`.
3. Seed (listener start only): tasks workers report as active or reserved
   get a row if they have none.

`queued` and `retried` runs are never marked `lost`: they may be waiting in a
queue, which cannot be searched.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from celery_manager.db.models import TaskRun, WorkerState
from celery_manager.db.runs import (
    _select_locked,
    commit_with_deadlock_retry,
    lock_task_run,
)
from celery_manager.events.listener import (
    _merge_call_argument,
    _normalise_call_argument,
)
from celery_manager.redaction import redact_run

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("queued", "started", "retried", "lost")
_BACKEND_STATUS = {"SUCCESS": "succeeded", "FAILURE": "failed", "REVOKED": "revoked"}
LOST_REASON = "Lost: no worker holds this task and the broker does not"
_WORKER_FRESH_SECONDS = 120


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _backend(app):
    from celery.backends.base import DisabledBackend
    backend = app.backend
    return None if isinstance(backend, DisabledBackend) else backend


def _done_at(meta: dict) -> datetime | None:
    value = meta.get("date_done")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return _aware(value) if isinstance(value, datetime) else None


def _apply_backend(run: TaskRun, meta: dict, config) -> bool:
    status = _BACKEND_STATUS.get(meta.get("status"))
    if status is None or run.status not in OPEN_STATUSES:
        return False
    limit = config.result_max_bytes
    run.status, run.status_source = status, "backend"
    run.finished_at = _done_at(meta) or datetime.now(timezone.utc)
    result = meta.get("result")
    if status == "succeeded" and config.store_results and run.result_repr is None:
        from celery_manager.logcapture import result_fields
        for key, value in result_fields(result, config).items():
            setattr(run, key, value)
    elif status == "failed":
        run.exception = repr(result)[:limit]
        if meta.get("traceback"):
            run.traceback = str(meta["traceback"])[:limit]
    if run.task_name == "unknown" and meta.get("name"):
        run.task_name = meta["name"]
    if meta.get("args") is not None:
        run.args = _merge_call_argument(run.args, meta["args"], "[]")
    if meta.get("kwargs") is not None:
        run.kwargs = _merge_call_argument(run.kwargs, meta["kwargs"], "{}")
    redact_run(config, run)
    return True


def _held_task_ids(app, timeout: float, expected: set[str]) -> set[str] | None:
    """Task ids workers say they hold, or None unless every expected worker
    answered the ping and all three follow-up inspects."""
    names = sorted(app.control.inspect(timeout=timeout).ping() or {})
    if not names or expected - set(names):
        return None
    follow_up = app.control.inspect(destination=names, timeout=timeout, limit=len(names))
    held: set[str] = set()
    for call in (follow_up.active, follow_up.reserved, follow_up.scheduled):
        replies = call()
        if not replies or set(names) - set(replies):
            return None
        for items in replies.values():
            for item in items or []:
                request = item.get("request", item) if call.__name__ == "scheduled" else item
                if isinstance(request, dict) and request.get("id"):
                    held.add(request["id"])
    return held


def _broker_task_ids(app, session, timeout: float) -> set[str] | None:
    """Task ids in the Redis unacked set, or None when the broker cannot say."""
    from celery_manager.control.unacked import list_unacked
    listing = list_unacked(app, session, timeout)
    if not listing["supported"] or listing["total"] > len(listing["entries"]):
        return None
    if any(not e["task_id"] or e["message_missing"] for e in listing["entries"]):
        return None
    return {e["task_id"] for e in listing["entries"]}


def _lost_candidates_held(config, session) -> set[str] | None:
    """Ids that something still holds; None means "do not conclude anything"."""
    app, timeout = config.celery_app, config.inspect_timeout_seconds
    fresh = datetime.now(timezone.utc) - timedelta(seconds=_WORKER_FRESH_SECONDS)
    expected = {w.hostname for w in session.query(WorkerState).filter(WorkerState.state == "online")
                if _aware(w.last_seen_at) >= fresh}
    try:
        workers = _held_task_ids(app, timeout, expected)
        broker = _broker_task_ids(app, session, timeout) if workers is not None else None
    except Exception:
        logger.warning("celery-manager reconcile: could not read workers or broker", exc_info=True)
        return None
    return None if workers is None or broker is None else workers | broker


def reconcile_once(config, session_factory, *, now: datetime | None = None) -> dict:
    """One pass over runs open for at least `reconcile_after_seconds`.
    Returns counts: `checked`, `from_backend`, `lost`."""
    app = config.celery_app
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=config.reconcile_after_seconds)
    backend = _backend(app)
    counts = {"checked": 0, "from_backend": 0, "lost": 0}
    still_started: list[str] = []
    last_id = 0
    while True:
        with session_factory() as session:
            rows = (session.query(TaskRun.id, TaskRun.task_id)
                    .filter(TaskRun.source == "celery", TaskRun.status.in_(OPEN_STATUSES),
                            TaskRun.updated_at < cutoff, TaskRun.id > last_id)
                    .order_by(TaskRun.id).limit(config.reconcile_batch_size).all())
            if not rows:
                break
            last_id = rows[-1].id
            for _, task_id in rows:
                counts["checked"] += 1
                meta = None
                if backend is not None:
                    try:
                        meta = backend.get_task_meta(task_id)
                    except Exception:  # noqa: BLE001 — one bad lookup must not stop the pass
                        logger.warning("celery-manager reconcile: backend lookup failed for %s", task_id)

                def write(task_id=task_id, meta=meta):
                    run = _select_locked(session, task_id)
                    if run is None:  # purged since the listing
                        return
                    if meta is not None and _apply_backend(run, meta, config):
                        counts["from_backend"] += 1
                    elif run.status == "started":
                        still_started.append(task_id)

                commit_with_deadlock_retry(session, write)
    if still_started:
        with session_factory() as session:
            held = _lost_candidates_held(config, session)
            if held is not None:
                for task_id in still_started:
                    if task_id in held:
                        continue

                    def mark(task_id=task_id):
                        run = _select_locked(session, task_id)
                        if run is not None and run.status == "started":  # a late event may have moved it on
                            run.status, run.status_source, run.exception = "lost", "reconcile", LOST_REASON
                            counts["lost"] += 1

                    commit_with_deadlock_retry(session, mark)
    return counts


def seed_inflight(config, session_factory) -> int:
    """Create rows for tasks workers report as active or reserved and that have none."""
    app, created = config.celery_app, 0
    inspect = app.control.inspect(timeout=config.inspect_timeout_seconds)
    for state, status in (("reserved", "queued"), ("active", "started")):
        replies = getattr(inspect, state)() or {}
        for worker, items in replies.items():
            for item in items or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                with session_factory() as session:
                    if session.query(TaskRun.id).filter(TaskRun.task_id == item["id"]).first() is not None:
                        continue

                    def write(item=item, worker=worker, status=status):
                        run = lock_task_run(session, item["id"], seed={"task_name": item.get("name") or "unknown", "status": status})
                        run.status_source, run.worker = "seed", worker
                        run.queue = (item.get("delivery_info") or {}).get("routing_key")
                        run.args = _normalise_call_argument(item.get("args", "[]"))
                        run.kwargs = _normalise_call_argument(item.get("kwargs", "{}"))
                        redact_run(config, run)
                        started = item.get("time_start")
                        if status == "started" and isinstance(started, (int, float)):
                            run.started_at = datetime.fromtimestamp(started, timezone.utc)

                    commit_with_deadlock_retry(session, write)
                    created += 1
    return created


def run_forever(config, session_factory, stop: threading.Event | None = None, *, seed: bool = False) -> None:
    stop = stop or threading.Event()
    interval = config.reconcile_interval_seconds or 60.0
    if seed:
        try:
            logger.info("celery-manager reconcile: seeded %d in-flight run(s)", seed_inflight(config, session_factory))
        except Exception:
            logger.warning("celery-manager reconcile: seeding failed", exc_info=True)
    while not stop.is_set():
        try:
            counts = reconcile_once(config, session_factory)
            if counts["from_backend"] or counts["lost"]:
                logger.info("celery-manager reconcile: %s", json.dumps(counts))
        except Exception:
            logger.exception("celery-manager reconcile: pass failed")
        stop.wait(interval)


def start_in_background(config, session_factory) -> tuple[threading.Thread, threading.Event] | None:
    """Run the reconciler next to the events listener. None when
    `reconcile_interval_seconds` is 0."""
    if not config.reconcile_interval_seconds:
        return None
    stop = threading.Event()
    thread = threading.Thread(target=run_forever, args=(config, session_factory, stop), kwargs={"seed": True},
                              name="cm-reconcile", daemon=True)
    thread.start()
    return thread, stop
