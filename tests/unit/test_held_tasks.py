"""Delayed (ETA/countdown) tasks and stuck/unacknowledged messages.

Workers are faked at `celery_app.control.inspect` (as in test_workers) and
Redis at `celery_app.connection_for_read` (as in test_queues), with a small
in-memory Redis that understands the handful of commands kombu's unacked
bookkeeping uses. The restore runs kombu's real `Channel._do_restore_message`
so the message format going back on the queue is kombu's, not ours. The same
flows run against real brokers in tests/integration/test_held_tasks_brokers.py.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from kombu.transport import virtual
from kombu.transport.redis import PRIORITY_STEPS
from kombu.transport.redis import Channel as RedisChannel

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import unacked as unacked_mod
from celery_manager.control.masking import mask_call
from celery_manager.control.tasks import MAX_BULK_CANCEL, BulkCancelTooLarge
from celery_manager.control.unacked import UnackedRefused, list_unacked, restore_unacked
from celery_manager.control.workers import (
    WorkerControlError,
    cancel_delayed_tasks,
    list_delayed_tasks,
    live_task_index,
)
from celery_manager.db.models import AuditLog, TaskRun

W1 = "celery@alpha"
W2 = "celery@beta"


# --- fakes --------------------------------------------------------------------


def _inspector(**replies):
    insp = MagicMock()
    for method in ("ping", "stats", "active", "reserved", "scheduled", "active_queues", "revoked"):
        getattr(insp, method).return_value = replies.get(method)
    return insp


class FakeRedis:
    """Just enough Redis for kombu's unacked hash/zset and queue lists."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.lists: dict[str, list[str]] = {}
        self.calls: list[tuple] = []

    # reads
    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zrange(self, key, start, end, withscores=False):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        items = items[start:] if end == -1 else items[start:end + 1]
        return [(k.encode(), s) for k, s in items] if withscores else [k.encode() for k, _ in items]

    def hmget(self, key, fields):
        h = self.hashes.get(key, {})
        return [h[f].encode() if f in h else None for f in fields]

    def hget(self, key, field):
        value = self.hashes.get(key, {}).get(field)
        return value.encode() if value is not None else None

    # writes
    def zrem(self, key, member):
        self.calls.append(("zrem", key, member))
        self.zsets.get(key, {}).pop(member, None)
        return self

    def hdel(self, key, field):
        self.calls.append(("hdel", key, field))
        self.hashes.get(key, {}).pop(field, None)
        return self

    def rpush(self, key, value):
        self.calls.append(("rpush", key))
        self.lists.setdefault(key, []).append(value)
        return self

    def lpush(self, key, value):
        self.calls.append(("lpush", key))
        self.lists.setdefault(key, []).insert(0, value)
        return self

    # transactions: commands after multi() are queued and applied on exec,
    # reads before it are immediate — redis-py's WATCH pipeline semantics.
    def transaction(self, fn, *watches):
        self.calls.append(("watch", *watches))
        pipe = FakePipe(self)
        fn(pipe)
        pipe.execute()


class FakePipe:
    def __init__(self, redis: FakeRedis):
        self.redis = redis
        self.queued: list | None = None

    def multi(self):
        self.queued = []

    @property
    def command_stack(self):
        return self.queued

    def __getattr__(self, name):
        target = getattr(self.redis, name)
        if self.queued is None:
            return target

        def queue(*args, **kwargs):
            self.queued.append((target, args, kwargs))
            return self

        return queue

    def execute(self):
        for target, args, kwargs in self.queued or []:
            target(*args, **kwargs)


class FakeChannel:
    """A kombu Redis channel minus the network, restoring through kombu's
    own `_do_restore_message`."""

    unacked_key = "unacked"
    unacked_index_key = "unacked_index"
    visibility_timeout = 3600
    global_keyprefix = ""
    ack_emulation = True
    priority_steps = PRIORITY_STEPS
    sep = RedisChannel.sep
    max_priority = RedisChannel.max_priority
    min_priority = RedisChannel.min_priority

    _do_restore_message = RedisChannel._do_restore_message
    _q_for_pri = RedisChannel._q_for_pri
    priority = RedisChannel.priority
    _get_message_priority = virtual.Channel._get_message_priority

    def __init__(self, redis: FakeRedis):
        self.client = redis

    def _lookup(self, exchange, routing_key, default=None):
        return [routing_key]

    @contextmanager
    def conn_or_acquire(self, client=None):
        yield self.client


def _message(task_id, task_name="demo.slow", queue="celery", eta=None):
    payload = {
        "body": "W1tdLCB7fSwge31d",
        "content-encoding": "utf-8",
        "content-type": "application/json",
        "headers": {"id": task_id, "task": task_name, "eta": eta},
        "properties": {
            "correlation_id": task_id,
            "delivery_info": {"exchange": "", "routing_key": queue},
            "priority": 0,
            "delivery_tag": f"tag-{task_id}",
        },
    }
    return json.dumps([payload, "", queue])


def _redis_with(*entries, now=None):
    """`entries`: (tag, task_id, age_seconds[, queue])."""
    redis = FakeRedis()
    now = now or datetime.now(timezone.utc).timestamp()
    for entry in entries:
        tag, task_id, age = entry[:3]
        queue = entry[3] if len(entry) > 3 else "celery"
        redis.hashes.setdefault("unacked", {})[tag] = _message(task_id, queue=queue)
        redis.zsets.setdefault("unacked_index", {})[tag] = now - age
    return redis


def _celery(redis: FakeRedis | None = None, insp=None, transport="redis", channel=None):
    app = MagicMock(spec=Celery)
    app.conf = MagicMock(broker_transport_options={})
    conn = MagicMock()
    conn.transport.driver_type = transport
    conn.default_channel = channel or FakeChannel(redis or FakeRedis())
    app.connection_for_read.return_value = conn
    app.control.inspect.return_value = insp or _inspector()
    return app


def _session_factory(celery_app=None):
    config = CeleryManagerConfig(
        celery_app=celery_app or Celery("t", broker="memory://"), db_url="sqlite:///:memory:", secret_key="test-secret",
    )
    _, session_factory = create_app(config)
    return session_factory


def _audit(session_factory, action):
    with session_factory() as s:
        return [json.loads(r.details) | {"_target": r.target_id} for r in s.query(AuditLog).filter_by(action=action)]


# --- masking -----------------------------------------------------------------


def test_mask_call_shows_shape_never_values():
    masked = mask_call(["alice@example.com", 42, [1, 2, 3], None], {"token": "s3cret", "opts": {"a": 1}})
    assert masked == {"args": ["str", "int", "list[3]", "None"], "kwargs": {"token": "str", "opts": "dict[1]"}}
    assert "alice" not in json.dumps(masked) and "s3cret" not in json.dumps(masked)


def test_mask_call_labels_unexpected_shapes_instead_of_guessing():
    assert mask_call("('x',)", None) == {"args": ["str"], "kwargs": {}}
    assert mask_call(None, "{'a': 1}") == {"args": [], "kwargs": {"?": "str"}}


# --- live task index ---------------------------------------------------------


def test_live_task_index_prefers_active_over_reserved_and_scheduled():
    insp = _inspector(
        ping={W1: {"ok": "pong"}, W2: {"ok": "pong"}},
        scheduled={W1: [{"eta": "x", "request": {"id": "t-eta"}}]},
        reserved={W1: [{"id": "t-res"}], W2: [{"id": "t-dup"}]},
        active={W1: [{"id": "t-dup"}]},
    )
    names, index = live_task_index(_celery(insp=insp), timeout=0.5)
    assert names == [W1, W2]
    assert index == {
        "t-eta": {"worker": W1, "state": "scheduled"},
        "t-res": {"worker": W1, "state": "reserved"},
        "t-dup": {"worker": W1, "state": "active"},
    }


def test_live_task_index_with_no_workers_is_empty():
    assert live_task_index(_celery(), timeout=0.5) == ([], {})


# --- delayed tasks: control --------------------------------------------------


def _scheduled_reply():
    return {
        W1: [{
            "eta": "2026-10-28T12:00:00+02:00",
            "priority": 6,
            "request": {
                "id": "t-late", "name": "demo.report", "args": ["alice@example.com"], "kwargs": {"n": 3},
                "delivery_info": {"routing_key": "reports", "priority": 6},
            },
        }],
        W2: [{
            "eta": "2026-10-28T09:30:00+00:00",
            "priority": 0,
            "request": {"id": "t-soon", "name": "demo.ping", "args": [], "kwargs": {}, "delivery_info": {}},
        }],
    }


def test_list_delayed_tasks_merges_workers_and_runs_soonest_first():
    insp = _inspector(ping={W1: {"ok": "pong"}, W2: {"ok": "pong"}}, scheduled=_scheduled_reply())
    sf = _session_factory()
    with sf() as session:
        session.add(TaskRun(task_id="t-soon", task_name="demo.ping", status="queued", queue="default", schedule_name="nightly"))
        session.commit()
        listing = list_delayed_tasks(_celery(insp=insp), session, timeout=0.5)

    assert [t["task_id"] for t in listing["tasks"]] == ["t-soon", "t-late"]
    soon, late = listing["tasks"]
    # The worker's +02:00 ETA is normalised to UTC.
    assert late["eta"] == "2026-10-28T10:00:00+00:00"
    assert late == late | {
        "task_name": "demo.report", "worker": W1, "queue": "reports", "priority": 6,
        "args_masked": {"args": ["str"], "kwargs": {"n": "int"}}, "args": None, "kwargs": None, "run": None,
    }
    # The worker didn't report a queue; the recorded run did.
    assert soon["queue"] == "default"
    assert soon["run"] == {"status": "queued", "schedule_name": "nightly"}
    assert listing["workers_replied"] == [W1, W2]


def test_list_delayed_tasks_reveals_args_only_when_asked():
    insp = _inspector(ping={W1: {"ok": "pong"}}, scheduled={W1: _scheduled_reply()[W1]})
    with _session_factory()() as session:
        [task] = list_delayed_tasks(_celery(insp=insp), session, timeout=0.5, reveal_args=True)["tasks"]
    assert task["args"] == ["alice@example.com"]
    assert task["kwargs"] == {"n": 3}


def test_list_delayed_tasks_reports_future_eta_runs_no_worker_holds():
    insp = _inspector(ping={W1: {"ok": "pong"}}, scheduled={W1: _scheduled_reply()[W1]})
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with _session_factory()() as session:
        session.add_all([
            TaskRun(task_id="t-late", task_name="demo.report", status="queued", eta=future),  # held: not repeated
            TaskRun(task_id="t-lost", task_name="demo.lost", status="queued", eta=future, queue="q"),
            TaskRun(task_id="t-past", task_name="demo.x", status="queued", eta=future - timedelta(days=1)),
            TaskRun(task_id="t-done", task_name="demo.x", status="succeeded", eta=future),
        ])
        session.commit()
        listing = list_delayed_tasks(_celery(insp=insp), session, timeout=0.5)
    assert [r["task_id"] for r in listing["unheld_runs"]] == ["t-lost"]
    assert listing["unheld_runs"][0]["queue"] == "q"
    assert listing["unheld_truncated"] is False


def test_list_delayed_tasks_with_no_workers_asks_only_once():
    app = _celery()
    with _session_factory()() as session:
        listing = list_delayed_tasks(app, session, timeout=0.5)
    assert listing["tasks"] == [] and listing["workers_replied"] == []
    assert app.control.inspect.call_count == 1


def test_list_delayed_tasks_wraps_broker_errors():
    insp = MagicMock()
    insp.ping.side_effect = ConnectionRefusedError("refused")
    with _session_factory()() as session, pytest.raises(WorkerControlError, match="Couldn't reach the Celery broker"):
        list_delayed_tasks(_celery(insp=insp), session, timeout=0.5)


def test_cancel_delayed_revokes_marks_live_runs_and_audits_as_cancel():
    sf = _session_factory()
    app = _celery()
    with sf() as session:
        session.add_all([
            TaskRun(task_id="t-q", task_name="x", status="queued"),
            TaskRun(task_id="t-r", task_name="x", status="retried"),
            TaskRun(task_id="t-done", task_name="x", status="succeeded"),
        ])
        session.commit()
        result = cancel_delayed_tasks(app, session, "alice", ["t-q", "t-r", "t-done", "t-unrecorded", "t-q"])

    assert result == {"revoked": 4, "runs_marked_revoked": 2}
    app.control.revoke.assert_called_once_with(["t-q", "t-r", "t-done", "t-unrecorded"])
    with sf() as session:
        status = dict(session.query(TaskRun.task_id, TaskRun.status).all())
        finished = session.query(TaskRun).filter_by(task_id="t-q").one().finished_at
    assert status == {"t-q": "revoked", "t-r": "revoked", "t-done": "succeeded"}
    assert finished is not None
    [row] = _audit(sf, "cancel_bulk")
    assert row["_target"] == "delayed"
    assert row["delayed"] is True and row["runs_marked_revoked"] == 2
    assert row["task_ids"] == ["t-q", "t-r", "t-done", "t-unrecorded"]


def test_cancel_one_delayed_task_writes_a_plain_cancel_row():
    sf = _session_factory()
    with sf() as session:
        cancel_delayed_tasks(_celery(), session, "alice", ["t-1"])
    [row] = _audit(sf, "cancel")
    assert row == {"terminate": False, "delayed": True, "run_marked_revoked": False, "_target": "t-1"}


def test_cancel_delayed_revokes_in_chunks():
    app = _celery()
    with _session_factory()() as session:
        cancel_delayed_tasks(app, session, "alice", [f"t-{i}" for i in range(250)])
    assert [len(c.args[0]) for c in app.control.revoke.call_args_list] == [100, 100, 50]


def test_cancel_delayed_refuses_empty_and_oversized_selections():
    with _session_factory()() as session:
        with pytest.raises(ValueError):
            cancel_delayed_tasks(_celery(), session, "alice", ["", ""])
        with pytest.raises(BulkCancelTooLarge):
            cancel_delayed_tasks(_celery(), session, "alice", [f"t-{i}" for i in range(MAX_BULK_CANCEL + 1)])


def test_cancel_delayed_marks_nothing_when_the_broadcast_fails():
    sf = _session_factory()
    app = _celery()
    app.control.revoke.side_effect = ConnectionRefusedError("refused")
    with sf() as session:
        session.add(TaskRun(task_id="t-q", task_name="x", status="queued"))
        session.commit()
        with pytest.raises(WorkerControlError):
            cancel_delayed_tasks(app, session, "alice", ["t-q"])
    with sf() as session:
        assert session.query(TaskRun).one().status == "queued"
        assert session.query(AuditLog).count() == 0


# --- unacked: control --------------------------------------------------------


def test_list_unacked_classifies_against_live_workers_oldest_first():
    redis = _redis_with(
        ("tag-new", "t-held", 5),
        ("tag-old", "t-orphan", 7200, "emails"),
        ("tag-mid", "t-running", 60),
    )
    insp = _inspector(
        ping={W1: {"ok": "pong"}},
        reserved={W1: [{"id": "t-held"}]},
        active={W1: [{"id": "t-running"}]},
    )
    sf = _session_factory()
    with sf() as session:
        session.add(TaskRun(task_id="t-orphan", task_name="demo.slow", status="started"))
        session.commit()
        listing = list_unacked(_celery(redis, insp), session, timeout=0.5)

    assert listing["supported"] is True
    assert listing["total"] == 3
    assert listing["visibility_timeout_seconds"] == 3600
    assert listing["workers_replied"] == [W1]
    old, mid, new = listing["entries"]
    assert [old["tag"], mid["tag"], new["tag"]] == ["tag-old", "tag-mid", "tag-new"]
    assert old | {"state": "orphaned", "worker": None, "queue": "emails", "task_name": "demo.slow",
                  "past_visibility_timeout": True, "run_status": "started"} == old
    assert old["age_seconds"] >= 7200
    assert mid["state"] == "running" and mid["worker"] == W1 and mid["worker_state"] == "active"
    assert new["state"] == "held" and new["worker_state"] == "reserved"
    assert new["past_visibility_timeout"] is False
    assert new["run_status"] is None


def test_list_unacked_uses_the_channels_key_names_and_visibility_timeout():
    redis = FakeRedis()
    redis.hashes["custom_unacked"] = {"t1": _message("t-1")}
    redis.zsets["custom_index"] = {"t1": datetime.now(timezone.utc).timestamp() - 120}
    channel = FakeChannel(redis)
    channel.unacked_key, channel.unacked_index_key, channel.visibility_timeout = "custom_unacked", "custom_index", 60
    with _session_factory()() as session:
        listing = list_unacked(_celery(channel=channel), session, timeout=0.5)
    [entry] = listing["entries"]
    assert entry["task_id"] == "t-1"
    assert entry["past_visibility_timeout"] is True
    assert listing["keys"]["unacked"] == "custom_unacked"


def test_full_key_adds_the_global_prefix_only_where_kombu_does_not():
    client = MagicMock(global_keyprefix="app:", PREFIXED_SIMPLE_COMMANDS=["HGET"], PREFIXED_COMPLEX_COMMANDS={})
    assert unacked_mod._full_key(client, "ZRANGE", "unacked_index") == "app:unacked_index"
    assert unacked_mod._full_key(client, "HGET", "unacked") == "unacked"
    assert unacked_mod._full_key(MagicMock(global_keyprefix=""), "ZRANGE", "unacked_index") == "unacked_index"


def test_list_unacked_keeps_the_listing_when_workers_cant_be_asked():
    insp = MagicMock()
    insp.ping.side_effect = ConnectionRefusedError("refused")
    with _session_factory()() as session:
        listing = list_unacked(_celery(_redis_with(("tag-1", "t-1", 10)), insp), session, timeout=0.5)
    assert listing["workers_error"]
    assert listing["entries"][0]["state"] == "unknown"


@pytest.mark.parametrize(("transport", "needle"), [("amqp", "RabbitMQ"), ("memory", "in-memory")])
def test_list_unacked_is_unsupported_off_redis(transport, needle):
    with _session_factory()() as session:
        listing = list_unacked(_celery(transport=transport), session, timeout=0.5)
    assert listing["supported"] is False
    assert needle in listing["reason"]


def test_list_unacked_is_unsupported_without_ack_emulation():
    channel = FakeChannel(FakeRedis())
    channel.ack_emulation = False
    with _session_factory()() as session:
        listing = list_unacked(_celery(channel=channel), session, timeout=0.5)
    assert listing["supported"] is False
    assert "ack_emulation" in listing["reason"]


def test_restore_pushes_the_message_back_the_way_kombu_does_and_audits():
    redis = _redis_with(("tag-1", "t-1", 7200, "emails"))
    sf = _session_factory()
    with sf() as session:
        result = restore_unacked(_celery(redis, _inspector(ping={W1: {"ok": "pong"}})), session, "alice", ["tag-1"], 0.5)

    assert result == {"restored": [{"tag": "tag-1", "queue": "emails", "task_id": "t-1"}], "missing": [], "refused": []}
    assert redis.hashes["unacked"] == {} and redis.zsets["unacked_index"] == {}
    [raw] = redis.lists["emails"]
    payload = json.loads(raw)
    # kombu's own restore marks the message redelivered and rpushes it, so it
    # is the next one a worker's BRPOP takes.
    assert payload["headers"]["redelivered"] is True
    assert payload["properties"]["delivery_info"]["redelivered"] is True
    assert ("rpush", "emails") in redis.calls
    # The whole thing ran under a WATCH on the unacked hash.
    assert ("watch", "unacked") in redis.calls
    [row] = _audit(sf, "restore_unacked")
    assert row["_target"] == "emails"
    assert row["restored"][0]["task_id"] == "t-1"


def test_restore_leaves_the_message_in_place_when_kombu_cannot_queue_it():
    class BrokenLookup(FakeChannel):
        def _lookup(self, exchange, routing_key, default=None):
            raise KeyError("no binding")

    redis = _redis_with(("tag-1", "t-1", 7200))
    sf = _session_factory()
    with sf() as session:
        result = restore_unacked(_celery(channel=BrokenLookup(redis)), session, "alice", ["tag-1"], 0.5)
    assert result["restored"] == []
    assert "left in place" in result["refused"][0]["reason"]
    # kombu alone would have deleted it here and lost the message.
    assert "tag-1" in redis.hashes["unacked"] and "tag-1" in redis.zsets["unacked_index"]
    assert _audit(sf, "restore_unacked") == []


def test_restore_refuses_a_running_task_even_with_force():
    redis = _redis_with(("tag-1", "t-1", 7200))
    insp = _inspector(ping={W1: {"ok": "pong"}}, active={W1: [{"id": "t-1"}]})
    sf = _session_factory()
    with sf() as session:
        result = restore_unacked(_celery(redis, insp), session, "alice", ["tag-1"], 0.5, force=True)
    assert result["restored"] == []
    assert result["refused"] == [{"tag": "tag-1", "task_id": "t-1", "reason": f"running on {W1}"}]
    assert "tag-1" in redis.hashes["unacked"] and redis.lists == {}
    assert _audit(sf, "restore_unacked") == []


def test_restore_of_a_held_task_needs_force():
    insp = _inspector(ping={W1: {"ok": "pong"}}, scheduled={W1: [{"eta": "x", "request": {"id": "t-1"}}]})
    redis = _redis_with(("tag-1", "t-1", 10))
    with _session_factory()() as session:
        refused = restore_unacked(_celery(redis, insp), session, "alice", ["tag-1"], 0.5)
        assert refused["refused"][0]["reason"].startswith(f"scheduled by {W1}")
        forced = restore_unacked(_celery(redis, insp), session, "alice", ["tag-1"], 0.5, force=True)
    assert [r["tag"] for r in forced["restored"]] == ["tag-1"]


def test_restore_reports_tags_already_gone_and_cleans_their_index():
    redis = _redis_with(("tag-1", "t-1", 10))
    redis.zsets["unacked_index"]["tag-dangling"] = 1.0
    with _session_factory()() as session:
        result = restore_unacked(_celery(redis), session, "alice", ["tag-1", "tag-dangling", "tag-never"], 0.5)
    assert [r["tag"] for r in result["restored"]] == ["tag-1"]
    assert result["missing"] == ["tag-dangling", "tag-never"]
    assert redis.zsets["unacked_index"] == {}


def test_restore_refuses_everything_when_workers_cant_be_asked():
    insp = MagicMock()
    insp.ping.side_effect = ConnectionRefusedError("refused")
    redis = _redis_with(("tag-1", "t-1", 10))
    with _session_factory()() as session, pytest.raises(UnackedRefused, match="would run it twice"):
        restore_unacked(_celery(redis, insp), session, "alice", ["tag-1"], 0.5)
    assert "tag-1" in redis.hashes["unacked"]


def test_restore_refuses_empty_selection():
    with _session_factory()() as session, pytest.raises(UnackedRefused):
        restore_unacked(_celery(), session, "alice", [""], 0.5)


# --- API ---------------------------------------------------------------------


def _client(role="admin", celery_app=None):
    celery_app = celery_app or _celery()
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw", role=role)
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory, celery_app


@pytest.mark.parametrize(("method", "path"), [
    ("get", "/api/delayed"), ("post", "/api/delayed/cancel"),
    ("get", "/api/queues/unacked"), ("post", "/api/queues/unacked/restore"),
])
def test_endpoints_require_auth(method, path):
    config = CeleryManagerConfig(celery_app=_celery(), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    response = getattr(TestClient(app, base_url="https://testserver"), method)(path)
    assert response.status_code == 401


def test_viewer_sees_delayed_tasks_masked_but_cannot_reveal_or_cancel():
    insp = _inspector(ping={W1: {"ok": "pong"}}, scheduled={W1: _scheduled_reply()[W1]})
    client, _, app = _client("viewer", _celery(insp=insp))
    body = client.get("/api/delayed").json()
    assert body["tasks"][0]["args"] is None
    assert body["tasks"][0]["args_masked"] == {"args": ["str"], "kwargs": {"n": "int"}}
    assert body["args_revealed"] is False
    assert "alice@example.com" not in json.dumps(body)

    reveal = client.get("/api/delayed?reveal_args=true")
    assert reveal.status_code == 403
    assert "operator" in reveal.json()["detail"]
    cancel = client.post("/api/delayed/cancel", json={"task_ids": ["t-late"]})
    assert cancel.status_code == 403
    app.control.revoke.assert_not_called()


def test_operator_reveals_and_cancels_delayed_tasks():
    insp = _inspector(ping={W1: {"ok": "pong"}}, scheduled={W1: _scheduled_reply()[W1]})
    client, sf, _ = _client("operator", _celery(insp=insp))
    assert client.get("/api/delayed?reveal_args=true").json()["tasks"][0]["args"] == ["alice@example.com"]
    response = client.post("/api/delayed/cancel", json={"task_ids": ["t-late"]})
    assert response.status_code == 200
    assert response.json() == {"revoked": 1, "runs_marked_revoked": 0}
    assert _audit(sf, "cancel")[0]["_target"] == "t-late"


def test_delayed_endpoints_answer_503_on_broker_errors_and_400_on_bad_input():
    insp = MagicMock()
    insp.ping.side_effect = ConnectionRefusedError("refused")
    app = _celery(insp=insp)
    app.control.revoke.side_effect = ConnectionRefusedError("refused")
    client, _, _ = _client("operator", app)
    assert client.get("/api/delayed").status_code == 503
    assert client.post("/api/delayed/cancel", json={"task_ids": ["t"]}).status_code == 503
    assert client.post("/api/delayed/cancel", json={"task_ids": []}).status_code == 422
    assert client.post("/api/delayed/cancel", json={"task_ids": ["", ""]}).status_code == 400


def test_unacked_endpoint_lists_for_viewers_and_says_when_unsupported():
    client, _, _ = _client("viewer", _celery(_redis_with(("tag-1", "t-1", 10))))
    body = client.get("/api/queues/unacked").json()
    assert body["supported"] is True
    assert body["entries"][0]["state"] == "orphaned"

    amqp_client, _, _ = _client("viewer", _celery(transport="amqp"))
    body = amqp_client.get("/api/queues/unacked").json()
    assert body["supported"] is False and "management" in body["reason"]


def test_unacked_endpoint_is_503_when_redis_cant_be_read():
    app = _celery()
    app.connection_for_read.side_effect = ConnectionRefusedError("refused")
    client, _, _ = _client("viewer", app)
    response = client.get("/api/queues/unacked")
    assert response.status_code == 503
    assert "could not reach the broker" in response.json()["detail"]


def test_restore_endpoint_needs_operator_and_reports_refusals():
    redis = _redis_with(("tag-1", "t-1", 10))
    viewer, _, _ = _client("viewer", _celery(redis))
    assert viewer.post("/api/queues/unacked/restore", json={"tags": ["tag-1"]}).status_code == 403
    assert "tag-1" in redis.hashes["unacked"]

    operator, _, _ = _client("operator", _celery(redis))
    response = operator.post("/api/queues/unacked/restore", json={"tags": ["tag-1"]})
    assert response.status_code == 200
    assert response.json()["restored"][0]["tag"] == "tag-1"


def test_restore_endpoint_is_409_when_workers_cant_be_asked():
    insp = MagicMock()
    insp.ping.side_effect = ConnectionRefusedError("refused")
    client, _, _ = _client("operator", _celery(_redis_with(("tag-1", "t-1", 10)), insp))
    response = client.post("/api/queues/unacked/restore", json={"tags": ["tag-1"]})
    assert response.status_code == 409
    assert "nothing was restored" in response.json()["detail"]
