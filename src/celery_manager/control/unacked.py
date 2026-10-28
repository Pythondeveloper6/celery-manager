"""Stuck and lost tasks on the Redis transport: messages a worker took but
never acknowledged.

Redis has no acknowledgements of its own, so kombu emulates them. When a
worker takes a message off a queue list, kombu writes the raw message into a
hash (`unacked`, keyed by delivery tag) and the time it was taken into a
sorted set (`unacked_index`); acking deletes both. If the worker dies first —
`kill -9`, OOM killer, a lost host — the message stays there. Some consumer
on the same broker periodically puts back entries older than the
`visibility_timeout` (default 3600s, `broker_transport_options`), so a lost
task is redelivered *eventually*, as long as some worker is still running;
until then it is invisible: not on the queue, not on any worker.

This module lists those entries, says which live worker (if any) still holds
each one, and puts chosen ones back on their queue now instead of waiting.

Everything is read from kombu's own `Channel` — key names (`unacked_key`,
`unacked_index_key`), the `global_keyprefix` (applied by the channel's
client), the visibility timeout — and the restore runs kombu's own
`Channel._do_restore_message` inside the same WATCH/MULTI transaction its
`QoS.restore_by_tag` uses, so the message goes back in exactly the format
and position (front of the line, `redelivered` set) kombu itself would put
it. Nothing here hardcodes a key or re-encodes a message.

Broker calls run under `queues._run_with_deadline`, so a broker that stops
answering can't pin the request.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from celery import Celery

from celery_manager.control.audit import record as record_audit
from celery_manager.control.queues import (
    QueueBrokerError,
    _open_connection,
    _run_with_deadline,
    _transport_name,
    _unsupported_reason,
)
from celery_manager.control.workers import live_task_index
from celery_manager.db.models import TaskRun

# Entries listed per request, oldest first. Thousands of unacked messages is
# itself the finding; the total is always reported.
MAX_LISTED = 500

# Tags one restore may carry. The same order of magnitude as a listing, so
# "restore everything shown" is always one request.
MAX_RESTORE = 500

_AMQP_REASON = (
    "RabbitMQ tracks unacknowledged messages itself and redelivers them as soon as the "
    "consumer's connection closes, so nothing gets stuck the way it can on Redis. The RabbitMQ "
    "management UI (and its API) shows unacknowledged counts per queue."
)


class UnackedRefused(ValueError):
    """The restore was refused as a whole (nothing was restored)."""


def _as_str(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _support(conn) -> tuple[object | None, str | None]:
    """The kombu Redis channel to work through, or why there isn't one."""
    transport = _transport_name(conn)
    reason = _unsupported_reason(transport)
    if reason:
        return None, reason
    if transport in ("amqp", "pyamqp", "librabbitmq"):
        return None, _AMQP_REASON
    channel = conn.default_channel
    # Duck-typed on the attributes this module uses rather than on the class,
    # so the Sentinel channel (a subclass) and a kombu that moved things
    # around are told apart by what they can actually do.
    if not all(hasattr(channel, a) for a in ("unacked_key", "unacked_index_key", "_do_restore_message", "client")):
        return None, (
            f"the {transport} transport doesn't keep a list of unacknowledged messages "
            "celery-manager can read; this view is for the Redis transport"
        )
    if not getattr(channel, "ack_emulation", True):
        return None, (
            "ack emulation is turned off (broker_transport_options ack_emulation=False), "
            "so kombu keeps no record of unacknowledged messages"
        )
    return channel, None


def _full_key(client, command: str, key: str) -> str:
    """`key` with the channel's `global_keyprefix`, for a read command the
    client won't prefix itself.

    kombu's prefixed client only prefixes the commands kombu uses (HGET,
    ZREM, WATCH, ...; its `PREFIXED_*_COMMANDS`). The listing needs ZCARD,
    ZRANGE and HMGET, which aren't on that list, so they get the prefix
    here — unless a newer kombu has added them, in which case the client
    does it and adding it again would double it.
    """
    prefix = getattr(client, "global_keyprefix", "") or ""
    handled = set(getattr(client, "PREFIXED_SIMPLE_COMMANDS", ())) | set(getattr(client, "PREFIXED_COMPLEX_COMMANDS", {}))
    return key if not prefix or command in handled else prefix + key


def _decode(raw) -> tuple[dict, str, str] | None:
    """kombu stores `json.dumps([payload, exchange, routing_key])`."""
    if raw is None:
        return None
    try:
        payload, exchange, routing_key = json.loads(_as_str(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload, exchange, routing_key


def _describe(tag: str, score: float, decoded, visibility_timeout: float, now: float) -> dict:
    payload, exchange, routing_key = decoded if decoded else ({}, None, None)
    headers = payload.get("headers") or {}
    properties = payload.get("properties") or {}
    age = max(0.0, now - score)
    return {
        "tag": tag,
        # Protocol 2 puts id/name in the headers; correlation_id carries the
        # id in both protocol versions, so it is the fallback.
        "task_id": headers.get("id") or properties.get("correlation_id"),
        "task_name": headers.get("task"),
        "queue": routing_key,
        "exchange": exchange,
        "eta": headers.get("eta"),
        "redelivered": bool(headers.get("redelivered") or (properties.get("delivery_info") or {}).get("redelivered")),
        "held_since": datetime.fromtimestamp(score, tz=timezone.utc).isoformat(),
        "age_seconds": round(age, 3),
        "past_visibility_timeout": age >= visibility_timeout,
        "message_missing": decoded is None,
    }


def _read_unacked(celery_app: Celery, limit: int) -> dict:
    conn = _open_connection(celery_app)
    try:
        channel, reason = _support(conn)
        if reason:
            return {"supported": False, "reason": reason, "transport": _transport_name(conn)}
        client = channel.client
        index_key, hash_key = channel.unacked_index_key, channel.unacked_key
        total = int(client.zcard(_full_key(client, "ZCARD", index_key)) or 0)
        # Ascending score = oldest first: the longest-held are the ones most
        # likely lost, and the ones kombu will restore first.
        pairs = client.zrange(_full_key(client, "ZRANGE", index_key), 0, limit - 1, withscores=True) if limit > 0 else []
        tags = [_as_str(tag) for tag, _ in pairs]
        raws = client.hmget(_full_key(client, "HMGET", hash_key), tags) if tags else []
        visibility_timeout = float(channel.visibility_timeout)
        now = datetime.now(timezone.utc).timestamp()
        entries = [
            _describe(tag, float(score), _decode(raw), visibility_timeout, now)
            for (tag, raw), (_, score) in zip(zip(tags, raws), pairs)
        ]
        return {
            "supported": True,
            "reason": None,
            "transport": _transport_name(conn),
            "visibility_timeout_seconds": visibility_timeout,
            "total": total,
            "entries": entries,
            "keys": {"unacked": hash_key, "unacked_index": index_key, "prefix": getattr(channel, "global_keyprefix", "") or ""},
        }
    finally:
        try:
            conn.release()
        except Exception:  # noqa: BLE001, S110 — releasing a broken connection is best-effort
            pass


def _classify(entry: dict, index: dict[str, dict]) -> None:
    """running / held / orphaned, from what live workers report."""
    held = index.get(entry["task_id"]) if entry["task_id"] else None
    if held is None:
        entry.update(state="orphaned", worker=None, worker_state=None)
    else:
        entry.update(
            state="running" if held["state"] == "active" else "held",
            worker=held["worker"],
            worker_state=held["state"],
        )


def _broker_call(fn, *args):
    try:
        return _run_with_deadline(fn, *args)
    except QueueBrokerError:
        raise
    except Exception as exc:  # unreachable, auth failure, bad URL: one broker error type
        raise QueueBrokerError(f"could not reach the broker: {exc}") from exc


def list_unacked(celery_app: Celery, session, timeout: float, limit: int = MAX_LISTED) -> dict:
    """Unacknowledged messages on the Redis transport, oldest first, each
    classified against what live workers report:

    - `running` — a live worker is executing it (an `acks_late` task, which
      stays unacked until it finishes). Leave it alone.
    - `held` — a live worker has it reserved (prefetched) or scheduled (an
      ETA/countdown task, which stays unacked until its ETA). Normal.
    - `orphaned` — no live worker reports it: its worker died. kombu will
      redeliver it once it is older than the visibility timeout *and* some
      worker on this broker is running to do so; `past_visibility_timeout`
      says whether the first half is true.

    Each entry also carries `run_status`, the status `task_runs` has for
    that task id (null if none is recorded): a `started` run whose message
    is orphaned is the classic "worker died mid-task" signature.

    If the worker inspect fails, entries are returned with state `unknown`
    and `workers_error` set, because the broker's own record is still worth
    showing.

    Non-Redis transports answer `{"supported": False, "reason": ...}`.
    Raises `QueueBrokerError` if Redis can't be read.
    """
    result = _broker_call(_read_unacked, celery_app, max(0, min(limit, MAX_LISTED)))
    if not result["supported"]:
        return result

    ids = [e["task_id"] for e in result["entries"] if e["task_id"]]
    statuses = dict(session.query(TaskRun.task_id, TaskRun.status).filter(TaskRun.task_id.in_(ids)).all()) if ids else {}
    for entry in result["entries"]:
        entry["run_status"] = statuses.get(entry["task_id"])

    result["workers_replied"] = None
    result["workers_error"] = None
    try:
        names, index = live_task_index(celery_app, timeout)
    except Exception as exc:  # noqa: BLE001 — WorkerControlError or anything inspect raises: report, keep the listing
        result["workers_error"] = str(exc)
        for entry in result["entries"]:
            entry.update(state="unknown", worker=None, worker_state=None)
        return result
    result["workers_replied"] = names
    for entry in result["entries"]:
        _classify(entry, index)
    return result


def _refusal(task_id: str | None, index: dict[str, dict], force: bool) -> str | None:
    """Why this message must not go back on the queue, or None."""
    held = index.get(task_id) if task_id else None
    if held is None:
        return None
    if held["state"] == "active":
        return f"running on {held['worker']}"
    if not force:
        return f"{held['state']} by {held['worker']}; send force to restore it anyway"
    return None


def _restore_on_broker(celery_app: Celery, tags: list[str], index: dict[str, dict], force: bool) -> dict:
    conn = _open_connection(celery_app)
    try:
        channel, reason = _support(conn)
        if reason:
            raise QueueBrokerError(reason)
        restored: list[dict] = []
        missing: list[str] = []
        refused: list[dict] = []
        for tag in tags:
            outcome: dict = {}

            # kombu's QoS.restore_by_tag, step for step, with the live-worker
            # check in the middle: the task id is only known once the message
            # is read, and reading it inside the transaction means the check
            # and the push are about the same message. `transaction` WATCHes
            # the hash and re-runs this function if another client changes it
            # before EXEC (kombu's own restore racing us, or the worker
            # finally acking), so the outcome is reset on every attempt and
            # the message is pushed back at most once. A tag whose message is
            # gone still has its index entry removed, as kombu does.
            def restore_transaction(pipe, tag=tag, outcome=outcome):
                outcome.clear()
                decoded = _decode(pipe.hget(channel.unacked_key, tag))
                task_id = (decoded[0].get("headers") or {}).get("id") if decoded else None
                refusal = _refusal(task_id, index, force)
                pipe.multi()
                if refusal:
                    outcome.update(refused=refusal, task_id=task_id)
                    return
                if decoded:
                    payload, exchange, routing_key = decoded
                    # kombu logs and swallows any error while queueing the
                    # push, then deletes the unacked copy regardless — the
                    # message is gone. Here the queued commands are counted
                    # instead: no push, no delete, and the entry stays put.
                    stack = getattr(pipe, "command_stack", None)
                    before = len(stack) if stack is not None else None
                    channel._do_restore_message(payload, exchange, routing_key, pipe, False)
                    if before is not None and len(stack) == before:
                        outcome.update(refused="kombu could not queue it (see the server log); left in place", task_id=task_id)
                        return
                    outcome.update(queue=routing_key, task_id=task_id)
                pipe.zrem(channel.unacked_index_key, tag)
                pipe.hdel(channel.unacked_key, tag)

            with channel.conn_or_acquire() as client:
                client.transaction(restore_transaction, channel.unacked_key)
            if "refused" in outcome:
                refused.append({"tag": tag, "task_id": outcome["task_id"], "reason": outcome["refused"]})
            elif outcome:
                restored.append({"tag": tag, **outcome})
            else:
                missing.append(tag)
        return {"restored": restored, "missing": missing, "refused": refused}
    finally:
        try:
            conn.release()
        except Exception:  # noqa: BLE001, S110 — best-effort release
            pass


def restore_unacked(
    celery_app: Celery,
    session,
    username: str,
    tags: list[str],
    timeout: float,
    force: bool = False,
) -> dict:
    """Put unacknowledged messages back on their queue now, at the front.

    Live workers are asked what they hold first (a fresh inspect, not
    whatever the dashboard last showed), and each message is checked
    against that as it is read:

    - a message a live worker is **running** is always refused — restoring
      it would run the task a second time, concurrently;
    - one a live worker has **reserved or scheduled** is refused unless
      `force` is set, for the same reason, only less certain (the worker
      may be wedged);
    - **orphaned** messages are restored.

    If the workers can't be asked at all the whole request is refused:
    "nobody is running this" can't be checked, and a duplicate run is the
    one outcome this must not cause. A worker that is alive but doesn't
    answer within `timeout` looks the same as a dead one; that is the limit
    of what inspect can tell. And an orphaned task may have run partway
    before its worker died: restoring runs it again from the start, which is
    only safe for idempotent tasks — the same caveat as kombu's own
    redelivery after the visibility timeout.

    Tags no longer in `unacked` (acked, or restored by kombu, in the
    meantime) are reported as `missing`. One `restore_unacked` audit row per
    call that restored anything.

    Raises `UnackedRefused` (empty or oversized selection, workers can't be
    asked), `QueueBrokerError` (Redis unreachable or timed out, or not a
    Redis broker).
    """
    tags = list(dict.fromkeys(t for t in tags if t))
    if not tags:
        raise UnackedRefused("send at least one tag")
    if len(tags) > MAX_RESTORE:
        raise UnackedRefused(f"{len(tags)} tags sent; restore is limited to {MAX_RESTORE} at a time")

    try:
        _, index = live_task_index(celery_app, timeout)
    except Exception as exc:  # any failure to ask is a refusal
        raise UnackedRefused(
            f"couldn't ask the workers what they are running ({exc}), so nothing was restored: "
            "restoring a task a worker is still running would run it twice"
        ) from exc

    outcome = _broker_call(_restore_on_broker, celery_app, tags, index, force)
    if outcome["restored"]:
        restored = outcome["restored"]
        record_audit(
            session, username, "restore_unacked",
            "queue", restored[0]["queue"] if len({r["queue"] for r in restored}) == 1 else "selection",
            {**outcome, "force": force},
        )
    return outcome

