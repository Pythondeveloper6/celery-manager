from __future__ import annotations

import ast
import json
from datetime import datetime, timezone

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.runs import lock_task_run

_STATUS_BY_EVENT = {
    "task-sent": "queued",
    "task-received": "queued",
    "task-started": "started",
    "task-succeeded": "succeeded",
    "task-failed": "failed",
    "task-retried": "retried",
    "task-revoked": "revoked",
}

# Statuses the decorator owns outright and that no Celery lifecycle event can
# describe, so the listener must never overwrite them.
#
# `skipped` means "@track matched a schedule override and never called the task
# body". Celery knows nothing about that: the wrapper still returns normally,
# so the worker emits a perfectly ordinary task-succeeded for the same uuid.
# In the documented two-process topology (worker + a separate
# `celery-manager events` process) that event lands after the decorator's
# write and used to flip the row back to `succeeded` — so a paused occurrence
# displayed as if it had run, exactly whenever the listener is running.
#
# Deliberately narrow: only `skipped`. `succeeded`/`failed`/`retried` are
# shared vocabulary where a later listener update is the normal, correct
# sequence, and blanket-guarding them would break the ordinary case. (Stale
# *pre*-terminal events are a different problem with a narrower guard; see
# `_accepts_status`.)
_DECORATOR_OWNED_STATUSES = frozenset({"skipped"})


# Where a status sits in a single attempt's life, for ordering stale events.
# Celery events reach us from more than one producer — `task-sent` from the
# client that dispatched, everything else from the worker — and also race the
# decorator's direct writes, so "last event processed wins" is not "latest
# state wins". The observed failure: a `task-sent` processed after the worker
# had already started (or finished) the run flipped it back to `queued`, and
# nothing later ever corrected it.
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "revoked"})


def _accepts_status(current: str | None, incoming: str) -> bool:
    """Whether a lifecycle event's status may replace the row's current one.

    Only *backwards* moves to a pre-terminal state are refused:

    - `queued` (task-sent / task-received) only ever replaces `queued`. It
      is the earliest state there is, so seeing it over anything else means
      the event is stale. That includes `retried`: the next attempt's
      task-received leaves the row showing `retried` until it actually
      starts, which is accurate.
    - `started` never replaces a terminal status — a task-started landing
      after task-succeeded/the decorator's outcome is stale. It *does*
      replace `retried`, which is the ordinary next attempt.

    Terminal statuses and `retried` are applied as before (later wins),
    including `retried` over `failed` — see
    test_normal_status_transitions_are_unaffected_by_the_skipped_guard — and
    `skipped` stays untouchable (_DECORATOR_OWNED_STATUSES).

    Known gap, accepted: an undecorated task redelivered under the same id
    after a terminal `failed` (acks_late + worker lost) keeps showing
    `failed` until its new outcome arrives. A decorated one is corrected
    straight away by the decorator's own `started` write, which is not
    subject to this guard.
    """
    if current in _DECORATOR_OWNED_STATUSES:
        return False
    if incoming == "queued":
        return current in (None, "queued")
    if incoming == "started":
        return current not in _TERMINAL_STATUSES
    return True


def _is_json(value: str | None) -> bool:
    if value is None:
        return False
    try:
        json.loads(value)
    except ValueError:
        return False
    return True


def _normalise_call_argument(value) -> str:
    """Store event args/kwargs as JSON, the format the decorator writes.

    Celery events carry them as *reprs* — `"('x',)"`, `"{'b': 2}"` — and
    storing those verbatim gave the table two formats for one column: search
    (`q=`) matched differently by writer, and every reader had to know to try
    `ast.literal_eval` after `json.loads`. `literal_eval` is the safe
    evaluator (literals only, never `eval`). Celery truncates long reprs
    (`argsrepr_maxsize`), and some literals have no JSON form (sets, bytes,
    non-string keys); for those the original string is kept, and
    `control.tasks._decode_stored_call_argument` still refuses to guess.
    """
    if not isinstance(value, str):
        try:
            return json.dumps(list(value) if isinstance(value, tuple) else value)
        except (TypeError, ValueError):
            return str(value)
    try:
        decoded = ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return value
    if isinstance(decoded, tuple):
        decoded = list(decoded)
    try:
        return json.dumps(decoded)
    except (TypeError, ValueError):
        return value


def _merge_call_argument(existing: str | None, incoming, empty: str) -> str | None:
    """Pick the stored args/kwargs, given what the row has and what the event
    says.

    The decorator records the real call as JSON from inside the worker,
    whereas the event only has a possibly-truncated repr — so a non-empty
    JSON value already on the row is kept. The event's value wins only when
    the row has nothing useful: no value, the column default, or a non-JSON
    repr from an older listener.
    """
    if incoming is None:
        return existing
    if _is_json(existing) and existing != empty:
        return existing
    return _normalise_call_argument(incoming)


def _event_time(event: dict) -> datetime | None:
    """The event's own timestamp (Celery stamps every event with epoch
    seconds, from the sender's clock), or None if it has none."""
    value = event.get("timestamp")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _event_eta(event: dict) -> datetime | None:
    """task-sent / task-received carry `eta` as an ISO string (None when the
    task has no ETA or countdown)."""
    value = event.get("eta")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _earliest(current: datetime | None, incoming: datetime | None) -> datetime | None:
    """Keep the earlier of two instants. SQLite hands stored datetimes back
    naive (they were written as UTC), so both sides are made aware first."""
    if incoming is None:
        return current
    if current is None:
        return incoming
    current_utc = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current
    return incoming if incoming < current_utc else current


def upsert_task_run(session, event: dict) -> None:
    """Apply a single Celery event to the task_runs table.

    Pure function over a SQLAlchemy session so it's testable without a
    real broker/receiver — EventListener.run() below just feeds it events.

    Get-or-create goes through `db.runs.lock_task_run`, so a row the
    decorator or `retry_task` inserted between our lookup and our commit is
    merged into rather than colliding on the UNIQUE task_id — which used to
    raise out of the handler and drop the event, worker/queue and all.
    Fields are merged, never nulled: an event that lacks a field (or carries
    None for it) leaves the stored value alone.
    """
    event_type = event["type"]
    status = _STATUS_BY_EVENT.get(event_type)
    if status is None:
        return  # worker-* events etc. are not task_runs concerns here

    task_id = event["uuid"]
    run = lock_task_run(session, task_id, seed={
        "task_name": event.get("name") or "unknown",
        "status": status,
    })
    status_applied = _accepts_status(run.status, status)
    if status_applied:
        run.status = status
    # else: a decorator-owned or later status is already there (see
    # _accepts_status). Only `status` and the lifecycle timestamps below are
    # protected — every other field still merges, so a skipped run keeps
    # picking up the worker/queue the listener knows about and the decorator
    # doesn't.

    if event.get("name"):
        run.task_name = event["name"]
    if event.get("queue"):
        run.queue = event["queue"]
    # Workflow lineage: task-sent / task-received carry it, later events don't,
    # so merge-never-null (a `None` or missing key keeps what is stored).
    # `group` is not in Celery's event payload; the decorator records it.
    for key in ("parent_id", "root_id", "group_id"):
        if event.get(key):
            setattr(run, key, event[key])
    # `hostname` on task-sent is the *producer's* (the API process or beat
    # that dispatched), not a worker's; recording it as `worker` showed the
    # wrong machine and, if task-sent arrived late, overwrote the real one.
    if event.get("hostname") and event_type != "task-sent":
        run.worker = event["hostname"]
    if "args" in event:
        run.args = _merge_call_argument(run.args, event["args"], "[]")
    if "kwargs" in event:
        run.kwargs = _merge_call_argument(run.kwargs, event["kwargs"], "{}")
    # Timestamps follow the status: a stale task-started must not move
    # started_at past a finish time already recorded.
    if status_applied and event_type == "task-started":
        run.started_at = datetime.now(timezone.utc)
    if status_applied and event_type in ("task-succeeded", "task-failed"):
        run.finished_at = datetime.now(timezone.utc)
    # Queue wait inputs. Merged outside the status guard on purpose: a
    # task-sent processed after the worker already started the run is stale
    # for *status* but its timestamp is exactly the fact we want. Earliest
    # wins, so a duplicate or late event can never push the send time later
    # (which would shrink the wait).
    stamp = _event_time(event)
    if event_type == "task-sent":
        run.sent_at = _earliest(run.sent_at, stamp)
    elif event_type == "task-received":
        run.received_at = _earliest(run.received_at, stamp)
    if event_type in ("task-sent", "task-received"):
        eta = _event_eta(event)
        if eta is not None:
            run.eta = eta
    elif event_type == "task-retried" and status_applied:
        # A retry is a new attempt that reuses the task id: the first
        # attempt's send time (and ETA) would make its wait look like the
        # whole time since the original send. Forget them; the new attempt's
        # task-received refills what it can, and a run with no send time is
        # simply left out of wait statistics.
        run.sent_at = None
        run.received_at = None
        run.eta = None
    if event.get("runtime") is not None:
        run.runtime_seconds = event["runtime"]
    if event.get("exception") is not None:
        run.exception = event["exception"]
    if event.get("traceback") is not None:
        run.traceback = event["traceback"]


class EventListener:
    """Long-running consumer of the Celery event bus. Run via the
    `celery-manager events` CLI command (Task 16 wires this up for the
    Django demo; see cli.py)."""

    def __init__(self, config: CeleryManagerConfig, session_factory, event_bus=None):
        # `event_bus` is accepted for compatibility and ignored. The listener
        # used to publish each event straight onto it, but the dashboard's
        # stream now hears every write through the task_runs change feed
        # (api/sse.py), which a direct publish would only duplicate.
        self._config = config
        self._session_factory = session_factory

    def run(self) -> None:
        app = self._config.celery_app
        state = app.events.State()

        def on_event(event):
            state.event(event)
            with self._session_factory() as session:
                upsert_task_run(session, event)
                session.commit()

        with app.connection() as connection:
            receiver = app.events.Receiver(connection, handlers={"*": on_event})
            receiver.capture(limit=None, timeout=None, wakeup=True)
