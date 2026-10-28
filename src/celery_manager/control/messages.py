"""Looking inside a queue: the messages waiting in it, and removing or moving one.

**Redis** keeps a queue as plain lists, one per priority step (kombu's
`PRIORITY_STEPS`, 0/3/6/9): the queue name itself for step 0 and
`<name>\\x06\\x16<step>` for the others, all under the transport's
`global_keyprefix`. Producers `LPUSH`; workers `BRPOP` the lists in step order,
lowest step first (on Redis a *lower* priority number runs sooner). So the
next message to run is the tail of the first non-empty list, and reading the
lists tail-first, step by step, gives the exact order workers will take them.
`LRANGE` reads without taking anything, so browsing Redis is exact and
side-effect free.

**RabbitMQ** has no way to read a message without taking it. The only option
is `basic_get` without ack, then reject with requeue: the message goes back,
but RabbitMQ marks it redelivered and may put it back at a different place.
That is a real change to the queue, so it is off unless the operator turns on
`queue_browser_amqp`, capped at a few messages, and labelled approximate.

Delete and move are Redis-only: they need to find one exact message and take
it out without disturbing the rest, which AMQP can't do.

Every broker call runs through `queues._run_with_deadline`, so a dead broker
can't hang the request.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from bisect import bisect
from dataclasses import dataclass
from datetime import datetime, timezone

from celery import Celery

from celery_manager.control.audit import record as record_audit
from celery_manager.control.queues import (
    QueueBrokerError,
    _is_not_found,
    _open_connection,
    _run_with_deadline,
    _transport_name,
    _unsupported_reason,
    _worker_cache,
    _worker_cache_lock,
    configured_queue_names,
    recorded_queue_names,
)
from celery_manager.db.models import TaskRun
from celery_manager.occurrences import SENT_AT_HEADER, parse_occurrence

# Page size bounds for Redis. Each message is decoded server-side, so a page is
# kept small enough to answer well inside the broker deadline.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# RabbitMQ: how many messages one look may take off the queue at once. Every
# one of them is invisible to workers until it is put back and comes back
# marked redelivered, so this stays small whatever the caller asks for.
AMQP_PEEK_CAP = 20

# Bodies larger than this are not decoded: a message of several megabytes
# would cost the API process memory and CPU to show arguments nobody can read
# in a table cell anyway.
MAX_DECODED_BODY = 64 * 1024

# Arguments are shown as text, cut to this many characters.
MAX_ARGS_CHARS = 2000

# Delete/move: how long the search for one message may run before giving up
# without changing anything. Below the broker deadline on purpose: the
# change must either happen inside the request or not at all, never later
# in a background thread after the request has already answered.
LOCATE_BUDGET = 2.0
# Redis items read per LRANGE while searching for one message.
LOCATE_CHUNK = 500
# WATCH aborts the change whenever the list moved under us (a push or a pop);
# that many fresh attempts before telling the caller to try again.
TAKE_ATTEMPTS = 5

AMQP_SIDE_EFFECTS = [
    "each message read is taken off the queue for a moment and put back",
    "messages read are marked redelivered",
    "messages read may move to the front of the queue",
]


class MessageNotFound(LookupError):
    """No waiting message with that task id in the queue."""


class MessageTaken(RuntimeError):
    """The message was there but a worker took it before we could."""


class MessageActionUnsupported(RuntimeError):
    """Delete/move asked of a broker that can't do it (anything but Redis)."""


class UnknownQueue(ValueError):
    """Move target that is neither configured nor seen."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Redis key layout --------------------------------------------------------


class _NoWatchError(Exception):
    """Default for `RedisLayout.watch_error`: never raised, so a layout built
    without one doesn't swallow real errors as "the list changed"."""


@dataclass
class RedisLayout:
    """How kombu's Redis transport names one queue's lists, for this broker.

    `client` is a plain redis-py client: kombu's prefixing client only
    prefixes the commands kombu itself uses (it leaves LRANGE and LREM
    alone), so keys are prefixed here, explicitly, for every command.
    """

    client: object
    prefix: str = ""
    sep: str = "\x06\x16"
    steps: tuple = (0, 3, 6, 9)
    # What the client raises when a WATCHed key changed before EXEC. Carried
    # here rather than imported at module level: redis-py is only installed
    # where the broker is Redis.
    watch_error: type = _NoWatchError

    def step_for(self, priority) -> int:
        # kombu clamps to 0..9 and treats a missing or odd value as 0.
        try:
            value = max(0, min(int(priority), 9))
        except (TypeError, ValueError):
            value = 0
        return self.steps[bisect(self.steps, value) - 1]

    def key(self, queue: str, step: int) -> str:
        return self.prefix + (f"{queue}{self.sep}{step}" if step else queue)

    def keys(self, queue: str) -> list[tuple[int, str]]:
        """(step, key) in the order workers consume them."""
        return [(step, self.key(queue, step)) for step in self.steps]


def _redis_layout(conn) -> RedisLayout:
    # Only reached on the Redis transport, which itself requires redis-py.
    import redis
    from redis.exceptions import WatchError

    channel = conn.default_channel
    return RedisLayout(
        client=redis.Redis(connection_pool=channel.pool),
        prefix=getattr(channel, "global_keyprefix", "") or "",
        sep=channel.sep,
        steps=tuple(channel.priority_steps),
        watch_error=WatchError,
    )


def _transport_kind(conn) -> str:
    name = _transport_name(conn)
    if name == "redis":
        return "redis"
    if name in ("amqp", "pyamqp", "librabbitmq"):
        return "amqp"
    return name


# --- decoding one message ----------------------------------------------------


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= MAX_ARGS_CHARS else text[:MAX_ARGS_CHARS] + "…"


def _decode_body(body, encoding: str | None, content_type: str | None):
    """The task call from the body, or (None, reason).

    JSON only. Pickle is never decoded here: unpickling a message would run
    whatever code it names, in the dashboard process.
    """
    if isinstance(body, str):
        if encoding == "base64":
            try:
                raw = base64.b64decode(body)
            except (binascii.Error, ValueError):
                return None, "the body isn't valid base64"
        else:
            raw = body.encode("utf-8")
    else:
        raw = bytes(body or b"")
    if len(raw) > MAX_DECODED_BODY:
        return None, f"the body is larger than {MAX_DECODED_BODY // 1024} KB, so it isn't decoded"
    if content_type and "json" not in content_type:
        return None, f"the body is {content_type}, which isn't decoded here"
    try:
        return json.loads(raw), None
    except (ValueError, UnicodeDecodeError):
        return None, "the body isn't valid JSON"


def _call_from_body(decoded) -> tuple[str | None, str | None, dict]:
    """(args, kwargs, extra fields) from a decoded body, protocol 1 or 2."""
    if isinstance(decoded, list) and len(decoded) >= 2:  # protocol 2: [args, kwargs, embed]
        return json.dumps(decoded[0]), json.dumps(decoded[1]), {}
    if isinstance(decoded, dict) and "task" in decoded:  # protocol 1 carries everything in the body
        extra = {k: decoded.get(k) for k in ("task", "id", "eta", "retries")}
        return json.dumps(decoded.get("args", [])), json.dumps(decoded.get("kwargs", {})), extra
    return None, None, {}


def describe_message(
    headers: dict | None,
    properties: dict | None,
    body,
    *,
    content_type: str | None,
    body_encoding: str | None,
    size_bytes: int,
    show_args: bool,
) -> dict:
    """One waiting message as the API shows it. Never includes the raw body."""
    headers = headers or {}
    properties = properties or {}
    task = headers.get("task")
    task_id = headers.get("id")
    eta = headers.get("eta")
    retries = headers.get("retries")

    args = kwargs = None
    note = None
    if show_args:
        # Protocol 2 puts a repr in the headers, already cut by the producer
        # (`argsrepr_maxsize`); cheaper and safer than decoding the body.
        args, kwargs = headers.get("argsrepr"), headers.get("kwargsrepr")
    if (show_args and args is None) or task is None:
        decoded, note = _decode_body(body, body_encoding, content_type)
        if decoded is not None:
            body_args, body_kwargs, extra = _call_from_body(decoded)
            task = task or extra.get("task")
            task_id = task_id or extra.get("id")
            eta = eta or extra.get("eta")
            retries = retries if retries is not None else extra.get("retries")
            if show_args and args is None:
                args, kwargs = body_args, body_kwargs
                if args is None:
                    note = "this message isn't a Celery task call"
    if not show_args:
        note = None

    sent_at = parse_occurrence(headers.get(SENT_AT_HEADER)) if headers.get(SENT_AT_HEADER) else None
    return {
        "task_id": task_id,
        "task_name": task or None,
        "eta": eta,
        "retries": retries,
        "priority": properties.get("priority"),
        "size_bytes": size_bytes,
        "sent_at": sent_at.isoformat() if sent_at else None,
        "args": _clip(args) if show_args else None,
        "kwargs": _clip(kwargs) if show_args else None,
        "args_note": note,
    }


def _describe_redis_item(raw: bytes, show_args: bool) -> dict:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    properties = payload.get("properties") or {}
    return describe_message(
        payload.get("headers"),
        properties,
        payload.get("body"),
        content_type=payload.get("content-type"),
        body_encoding=properties.get("body_encoding"),
        size_bytes=len(raw),
        show_args=show_args,
    )


# --- Redis: peek -------------------------------------------------------------


def redis_peek(layout: RedisLayout, queue: str, limit: int, offset: int, show_args: bool) -> dict:
    """A page of `queue` in consumption order. Read-only (LLEN + LRANGE).

    Indexes are counted from the tail (negative), which is where workers
    take from: a producer pushing at the head meanwhile doesn't shift them.
    """
    keys = layout.keys(queue)
    pipe = layout.client.pipeline(transaction=False)
    for _, key in keys:
        pipe.llen(key)
    lengths = [int(n or 0) for n in pipe.execute()]
    total = sum(lengths)

    # Which slice of which list the requested page covers.
    plan = []
    start = offset
    remaining = limit
    for (step, key), length in zip(keys, lengths):
        if remaining <= 0:
            break
        if start >= length:
            start -= length
            continue
        take = min(length - start, remaining)
        plan.append((step, key, start, take))
        remaining -= take
        start = 0

    pipe = layout.client.pipeline(transaction=False)
    for _, key, first, take in plan:
        # Consumption positions first..first+take-1 are tail indexes
        # -(first+1) down to -(first+take); LRANGE returns them head-first.
        pipe.lrange(key, -(first + take), -(first + 1))
    chunks = pipe.execute() if plan else []

    messages = []
    position = offset
    for (step, _key, _first, _take), items in zip(plan, chunks):
        for raw in reversed(items or []):
            message = _describe_redis_item(raw, show_args)
            message["position"] = position
            message["priority_step"] = step
            messages.append(message)
            position += 1
    return {"total": total, "messages": messages}


# --- Redis: delete / move ----------------------------------------------------


def _locate(layout: RedisLayout, queue: str, task_id: str, deadline: float):
    """(key, tail index, raw item, payload) of the waiting message, or None."""
    needle = task_id.encode("utf-8")
    for _step, key in layout.keys(queue):
        index = 0
        while True:
            if time.monotonic() > deadline:
                raise QueueBrokerError(
                    "the queue is too long to search for one message in time; nothing was changed"
                )
            items = layout.client.lrange(key, -(index + LOCATE_CHUNK), -(index + 1))
            if not items:
                break
            for offset, raw in enumerate(reversed(items)):
                # A substring test first: decoding every item of a long queue
                # to compare ids would cost far more than it finds.
                if needle not in raw:
                    continue
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if (payload.get("headers") or {}).get("id") == task_id:
                    return key, -(index + offset + 1), raw, payload
            if len(items) < LOCATE_CHUNK:
                break
            index += LOCATE_CHUNK
    return None


def _retarget(payload: dict, target: str) -> bytes:
    """The payload as if it had been sent to `target` in the first place.

    kombu re-delivers a message a worker took but never acknowledged by
    looking up `delivery_info` (exchange, routing key). Left pointing at the
    old queue, a moved message that hit a worker crash would quietly return
    to the queue it was moved out of. The anonymous exchange with the queue
    name as routing key is what kombu uses for a plain `queue=` send.
    """
    properties = payload.setdefault("properties", {})
    info = properties.setdefault("delivery_info", {})
    info["exchange"] = ""
    info["routing_key"] = target
    return json.dumps(payload).encode("utf-8")


def redis_take(layout: RedisLayout, queue: str, task_id: str, target: str | None = None) -> dict:
    """Remove the waiting message for `task_id` from `queue`; with `target`,
    push it onto that queue instead of dropping it.

    The exact stored string is removed (`LREM` count 1), never "a message
    with this id" re-encoded, so nothing else in the list is touched. The
    read and the change are one WATCH/MULTI transaction on the source list:
    if anything pops or pushes between our check and EXEC, EXEC does
    nothing and we look again. That is what keeps a message a worker took
    meanwhile from being moved anyway (a duplicate) or counted as deleted.

    Raises MessageNotFound if it was never there, MessageTaken if it was
    there and a worker took it before we could.
    """
    deadline = time.monotonic() + LOCATE_BUDGET
    seen = False
    for _attempt in range(TAKE_ATTEMPTS):
        found = _locate(layout, queue, task_id, deadline)
        if found is None:
            if seen:
                raise MessageTaken(task_id)
            raise MessageNotFound(task_id)
        seen = True
        key, index, raw, payload = found
        moved = None
        target_key = None
        if target is not None:
            target_key = layout.key(target, layout.step_for((payload.get("properties") or {}).get("priority")))
            moved = _retarget(payload, target)
        with layout.client.pipeline(transaction=True) as pipe:
            try:
                pipe.watch(key)
                # Still where we found it? (WATCH covers everything after this.)
                if pipe.lindex(key, index) != raw:
                    continue
                pipe.multi()
                pipe.lrem(key, 1, raw)
                if target_key is not None:
                    pipe.lpush(target_key, moved)
                results = pipe.execute()
            except layout.watch_error:
                continue
        if results and int(results[0]) == 1:
            return {"task_id": task_id, "task_name": (payload.get("headers") or {}).get("task")}
        # Removed by someone else between WATCH and EXEC can't happen (EXEC
        # would have aborted); a 0 here means the item was never in the list.
        raise MessageTaken(task_id)
    if seen:
        # Still there but the list kept changing under us: a busy queue.
        raise QueueBrokerError("the queue kept changing while we tried; nothing was changed, try again")
    raise MessageNotFound(task_id)


# --- RabbitMQ: approximate peek -------------------------------------------


def amqp_peek(conn, queue: str, limit: int, offset: int, show_args: bool) -> dict:
    """Take up to offset+limit (capped) messages without ack, then put them
    all back. Held unacknowledged until the end so each `basic_get` returns
    the next message, not the one just returned."""
    wanted = min(offset + limit, AMQP_PEEK_CAP)
    total = None
    probe = conn.channel()
    try:
        total = int(probe.queue_declare(queue=queue, passive=True).message_count)
    except Exception as exc:
        if _is_not_found(exc):  # not declared yet: nothing is waiting in it
            return {"total": 0, "messages": []}
        raise
    finally:
        try:
            probe.close()
        except Exception:  # noqa: BLE001, S110 — a 404 already closed it
            pass

    channel = conn.channel()
    taken = []
    try:
        while len(taken) < wanted:
            message = channel.basic_get(queue=queue, no_ack=False)
            if message is None:
                break
            taken.append(message)
        messages = []
        for position, message in enumerate(taken[offset:], start=offset):
            props = dict(getattr(message, "properties", {}) or {})
            body = message.body
            described = describe_message(
                props.get("application_headers") or getattr(message, "headers", None),
                props,
                body,
                content_type=props.get("content_type"),
                body_encoding=None,
                size_bytes=len(body or b""),
                show_args=show_args,
            )
            described["position"] = position
            described["priority_step"] = None
            messages.append(described)
        return {"total": total, "messages": messages}
    finally:
        for message in taken:
            try:
                channel.basic_reject(message.delivery_tag, requeue=True)
            except Exception:  # noqa: BLE001, S110 — closing the channel below requeues anything left
                pass
        try:
            channel.close()
        except Exception:  # noqa: BLE001, S110 — best-effort close
            pass


# --- public API --------------------------------------------------------------


def _release(conn) -> None:
    try:
        conn.release()
    except Exception:  # noqa: BLE001, S110 — releasing a broken connection is best-effort
        pass


def _peek_on_broker(celery_app: Celery, queue: str, limit: int, offset: int, show_args: bool, allow_amqp: bool) -> dict:
    conn = _open_connection(celery_app)
    try:
        transport = _transport_name(conn)
        base = {"transport": transport, "supported": True, "reason": None, "approximate": False, "side_effects": []}
        unsupported = _unsupported_reason(transport)
        if unsupported:
            return {**base, "supported": False, "reason": unsupported, "total": None, "messages": []}
        kind = _transport_kind(conn)
        if kind == "redis":
            return {**base, **redis_peek(_redis_layout(conn), queue, limit, offset, show_args)}
        if kind == "amqp":
            if not allow_amqp:
                return {
                    **base,
                    "supported": False,
                    "reason": (
                        "on RabbitMQ a message can only be read by taking it off the queue and putting it back, "
                        "which marks it redelivered and can change its place; an operator can turn this on "
                        "with queue_browser_amqp"
                    ),
                    "total": None,
                    "messages": [],
                }
            return {
                **base,
                "approximate": True,
                "side_effects": list(AMQP_SIDE_EFFECTS),
                **amqp_peek(conn, queue, limit, offset, show_args),
            }
        return {
            **base,
            "supported": False,
            "reason": f"browsing messages needs Redis or RabbitMQ; this broker uses the {transport} transport",
            "total": None,
            "messages": [],
        }
    finally:
        _release(conn)


def peek_messages(
    celery_app: Celery,
    session,
    queue: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    *,
    show_args: bool = True,
    allow_amqp: bool = False,
) -> dict:
    """Messages waiting in `queue`, in the order workers will take them.

    Each one is matched to its recorded run by task id (status, and a send
    time where one is known) so the dashboard can link to it. Raises
    `QueueBrokerError` when the broker can't be asked.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    try:
        result = _run_with_deadline(_peek_on_broker, celery_app, queue, limit, offset, show_args, allow_amqp)
    except QueueBrokerError:
        raise
    except Exception as exc:  # unreachable, auth failure, bad URL: one broker error type
        raise QueueBrokerError(f"could not reach the broker: {exc}") from exc

    messages = result["messages"]
    ids = [m["task_id"] for m in messages if m.get("task_id")]
    runs = {run.task_id: run for run in session.query(TaskRun).filter(TaskRun.task_id.in_(ids)).all()} if ids else {}
    now = _now()
    for message in messages:
        run = runs.get(message.get("task_id"))
        message["run"] = {"status": run.status} if run else None
        sent_at = message.get("sent_at")
        if sent_at is None and run is not None:
            # A send time recorded on the run, when the run has one: beat's
            # stamp for scheduled runs, and `sent_at` where the schema has it.
            recorded = getattr(run, "sent_at", None) or run.beat_sent_at
            if recorded is not None:
                if recorded.tzinfo is None:  # SQLite hands back naive UTC
                    recorded = recorded.replace(tzinfo=timezone.utc)
                sent_at = recorded.isoformat()
                message["sent_at"] = sent_at
        if sent_at:
            parsed = datetime.fromisoformat(sent_at)
            message["age_seconds"] = max(0.0, (now - parsed).total_seconds())
        else:
            message["age_seconds"] = None

    return {
        "queue": queue,
        "transport": result["transport"],
        "supported": result["supported"],
        "reason": result["reason"],
        "approximate": result["approximate"],
        "side_effects": result["side_effects"],
        "args_visible": show_args,
        "total": result["total"],
        "offset": offset,
        "limit": AMQP_PEEK_CAP if result["approximate"] else limit,
        "messages": messages,
    }


def known_queue_names(celery_app: Celery, session) -> set[str]:
    """Queues a move may target: configured, seen on a run, or (from the
    last cached worker reply, never a fresh broadcast) consumed by a worker.
    A typo must not quietly create a queue nobody reads."""
    names = set(configured_queue_names(celery_app)) | recorded_queue_names(session)
    with _worker_cache_lock:
        cached = _worker_cache.get(id(celery_app))
    if cached:
        names |= cached[1]
    return names


def _take_on_broker(celery_app: Celery, queue: str, task_id: str, target: str | None) -> dict:
    conn = _open_connection(celery_app)
    try:
        if _transport_kind(conn) != "redis":
            raise MessageActionUnsupported(
                f"deleting or moving one message needs Redis; this broker uses the {_transport_name(conn)} transport"
            )
        return redis_take(_redis_layout(conn), queue, task_id, target)
    finally:
        _release(conn)


def _take(celery_app: Celery, queue: str, task_id: str, target: str | None) -> dict:
    try:
        return _run_with_deadline(_take_on_broker, celery_app, queue, task_id, target)
    except (QueueBrokerError, MessageNotFound, MessageTaken, MessageActionUnsupported):
        raise
    except Exception as exc:  # unreachable, auth failure, bad URL: one broker error type
        raise QueueBrokerError(f"could not reach the broker: {exc}") from exc


def delete_message(celery_app: Celery, session, queue: str, task_id: str, username: str) -> dict:
    """Remove one waiting message for good (Redis). Audited as `delete_message`.

    Its run, if recorded and still `queued`, is marked `revoked`, as a purge
    does: the message will never reach a worker, so no event will ever
    settle the row.
    """
    taken = _take(celery_app, queue, task_id, None)
    run = session.query(TaskRun).filter(TaskRun.task_id == task_id).one_or_none()
    marked = False
    if run is not None and run.status == "queued":
        run.status = "revoked"
        run.finished_at = _now()
        marked = True
        session.commit()
    record_audit(session, username, "delete_message", "queue", queue, {
        "task_id": task_id,
        "task_name": taken.get("task_name"),
        "run_marked_revoked": marked,
    })
    return {"queue": queue, "task_id": task_id, "run_marked_revoked": marked}


def move_message(celery_app: Celery, session, queue: str, task_id: str, target: str, username: str) -> dict:
    """Move one waiting message to another known queue (Redis), keeping its
    priority. Audited as `move_message`; the recorded run's queue follows.

    Raises UnknownQueue for a target that isn't configured or seen, and
    ValueError for a move onto the same queue.
    """
    if target == queue:
        raise ValueError("the message is already in that queue")
    if target not in known_queue_names(celery_app, session):
        raise UnknownQueue(target)
    taken = _take(celery_app, queue, task_id, target)
    run = session.query(TaskRun).filter(TaskRun.task_id == task_id).one_or_none()
    if run is not None:
        run.queue = target
        session.commit()
    record_audit(session, username, "move_message", "queue", queue, {
        "task_id": task_id,
        "task_name": taken.get("task_name"),
        "from": queue,
        "to": target,
    })
    return {"queue": queue, "task_id": task_id, "target": target}
