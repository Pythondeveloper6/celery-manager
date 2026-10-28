"""Browsing, deleting and moving the messages waiting in a queue.

Redis is replaced by a tiny in-memory fake with list semantics and
WATCH/MULTI (fakeredis isn't a dependency), installed at the one seam the
control layer uses (`messages._open_connection` + `messages._redis_layout`).
Hooks on the fake simulate a worker popping a message between our read and
our change, which is the race delete/move must not get wrong.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from kombu import Queue

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import messages as messages_mod
from celery_manager.control import queues as queues_mod
from celery_manager.control.messages import RedisLayout, describe_message
from celery_manager.db.models import AuditLog, TaskRun, User

SEP = "\x06\x16"


class WatchError(Exception):
    """What redis-py raises when a WATCHed key changed before EXEC."""


# --- a tiny Redis ---------------------------------------------------------------


def _span(length: int, start: int, stop: int) -> tuple[int, int]:
    start = start + length if start < 0 else start
    stop = stop + length if stop < 0 else stop
    return max(start, 0), min(stop, length - 1)


class FakeRedis:
    """Lists stored head-first, as Redis does. `before_exec` runs just before
    a MULTI block executes and `after_watch` right after WATCH: tests use
    them to have a "worker" pop a message at the worst moment."""

    def __init__(self):
        self.lists: dict[str, list[bytes]] = {}
        self.versions: dict[str, int] = {}
        self.after_watch = None
        self.before_exec = None

    def _touch(self, key):
        self.versions[key] = self.versions.get(key, 0) + 1

    # commands
    def llen(self, key):
        return len(self.lists.get(key, []))

    def lrange(self, key, start, stop):
        items = self.lists.get(key, [])
        first, last = _span(len(items), start, stop)
        return list(items[first : last + 1]) if first <= last else []

    def lindex(self, key, index):
        items = self.lists.get(key, [])
        index = index + len(items) if index < 0 else index
        return items[index] if 0 <= index < len(items) else None

    def lpush(self, key, *values):
        for value in values:
            self.lists.setdefault(key, []).insert(0, value if isinstance(value, bytes) else value.encode())
        self._touch(key)
        return len(self.lists[key])

    def rpop(self, key):
        items = self.lists.get(key)
        if not items:
            return None
        self._touch(key)
        return items.pop()

    def lrem(self, key, count, value):
        items = self.lists.get(key, [])
        if value in items:
            items.remove(value)
            self._touch(key)
            return 1
        return 0

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    # helpers for tests
    def push_message(self, key, payload: dict) -> bytes:
        raw = json.dumps(payload).encode()
        self.lpush(key, raw)
        return raw


class FakePipeline:
    def __init__(self, redis: FakeRedis):
        self.redis = redis
        self.queued = []
        self.watched: dict[str, int] = {}
        self.immediate = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def watch(self, *keys):
        self.immediate = True
        for key in keys:
            self.watched[key] = self.redis.versions.get(key, 0)
        if self.redis.after_watch:
            hook, self.redis.after_watch = self.redis.after_watch, None
            hook()

    def multi(self):
        self.immediate = False

    def __getattr__(self, name):
        command = getattr(self.redis, name)

        def call(*args):
            if self.immediate:
                return command(*args)
            self.queued.append((command, args))
            return self

        return call

    def execute(self):
        if self.redis.before_exec:
            hook, self.redis.before_exec = self.redis.before_exec, None
            hook()
        for key, version in self.watched.items():
            if self.redis.versions.get(key, 0) != version:
                self.queued = []
                raise WatchError("watched key changed")
        results = [command(*args) for command, args in self.queued]
        self.queued = []
        return results


def _payload(task_id=None, task="demo.echo", priority=0, args=(1,), kwargs=None, queue="celery", headers=None):
    task_id = task_id or str(uuid.uuid4())
    body = json.dumps([list(args), kwargs or {}, {}]).encode()
    return {
        "body": base64.b64encode(body).decode(),
        "content-encoding": "utf-8",
        "content-type": "application/json",
        "headers": {
            "task": task,
            "id": task_id,
            "eta": None,
            "retries": 0,
            "argsrepr": repr(tuple(args)),
            "kwargsrepr": repr(kwargs or {}),
            **(headers or {}),
        },
        "properties": {
            "priority": priority,
            "body_encoding": "base64",
            "delivery_info": {"exchange": "", "routing_key": queue},
            "delivery_tag": str(uuid.uuid4()),
        },
    }


def _key(queue, step=0, prefix=""):
    return prefix + (f"{queue}{SEP}{step}" if step else queue)


def _send(redis: FakeRedis, queue: str, priority=0, prefix="", **kwargs) -> str:
    """Publish the way kombu does: LPUSH onto the list for the priority step."""
    payload = _payload(priority=priority, queue=queue, **kwargs)
    step = RedisLayout(client=None).step_for(priority)
    redis.push_message(_key(queue, step, prefix), payload)
    return payload["headers"]["id"]


# --- app plumbing -------------------------------------------------------------


@pytest.fixture
def redis():
    return FakeRedis()


@pytest.fixture
def broker(monkeypatch, redis):
    """The fake Redis behind the control layer, with optional key prefix."""
    state = {"prefix": "", "driver": "redis"}

    def open_connection(_app):
        conn = MagicMock()
        conn.transport = MagicMock(driver_type=state["driver"])
        return conn

    monkeypatch.setattr(messages_mod, "_open_connection", open_connection)
    monkeypatch.setattr(messages_mod, "_redis_layout", lambda conn: RedisLayout(client=redis, prefix=state["prefix"], watch_error=WatchError))
    queues_mod._worker_cache.clear()
    yield state
    queues_mod._worker_cache.clear()


def _make(runs=(), **config_kwargs):
    celery_app = Celery("test", broker="redis://localhost:1/0")
    celery_app.conf.task_queues = [Queue("celery"), Queue("bulk")]
    config = CeleryManagerConfig(
        celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret", **config_kwargs
    )
    app, session_factory = create_app(config)
    with session_factory() as session:
        for run in runs:
            session.add(run)
        session.commit()
    return app, session_factory


def _client(app, role="admin"):
    """A client signed in as an account named after its role (made on first use:
    each account costs a bcrypt hash, too slow to make three per test)."""
    with app.state.cm_session_factory() as session:
        if session.query(User).filter_by(username=role).one_or_none() is None:
            create_user(session, role, "pw", role=role)
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/auth/login", json={"username": role, "password": "pw"}).status_code == 200
    return client


# --- browsing -----------------------------------------------------------------


def test_browse_lists_messages_in_the_order_workers_take_them(broker, redis):
    app, _ = _make()
    low = _send(redis, "celery", priority=9)
    first = _send(redis, "celery")
    urgent = _send(redis, "celery", priority=3)
    second = _send(redis, "celery")
    middle = _send(redis, "celery", priority=5)  # step 3 too

    body = _client(app, "viewer").get("/api/queues/celery/messages").json()
    assert body["supported"] is True and body["approximate"] is False
    assert body["total"] == 5
    # Step 0 oldest first, then step 3 oldest first, then step 9.
    assert [m["task_id"] for m in body["messages"]] == [first, second, urgent, middle, low]
    assert [m["position"] for m in body["messages"]] == [0, 1, 2, 3, 4]
    assert [m["priority_step"] for m in body["messages"]] == [0, 0, 3, 3, 9]
    assert body["messages"][3]["priority"] == 5


def test_browse_takes_nothing_off_the_queue(broker, redis):
    app, _ = _make()
    for _ in range(3):
        _send(redis, "celery")
    before = {k: list(v) for k, v in redis.lists.items()}
    _client(app).get("/api/queues/celery/messages")
    assert redis.lists == before


def test_paging_crosses_priority_lists(broker, redis):
    app, _ = _make()
    ids = [_send(redis, "celery") for _ in range(3)] + [_send(redis, "celery", priority=6) for _ in range(3)]
    client = _client(app)
    page = client.get("/api/queues/celery/messages?limit=2&offset=2").json()
    assert [m["task_id"] for m in page["messages"]] == ids[2:4]
    assert [m["position"] for m in page["messages"]] == [2, 3]
    assert page["total"] == 6
    past_the_end = client.get("/api/queues/celery/messages?limit=10&offset=6").json()
    assert past_the_end["messages"] == [] and past_the_end["total"] == 6


def test_browse_respects_global_keyprefix(broker, redis):
    broker["prefix"] = "app1:"
    app, _ = _make()
    task_id = _send(redis, "celery", prefix="app1:")
    _send(redis, "celery")  # unprefixed: another app's queue of the same name
    body = _client(app).get("/api/queues/celery/messages").json()
    assert [m["task_id"] for m in body["messages"]] == [task_id]


def test_message_fields_and_the_recorded_run(broker, redis):
    sent_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    task_id = _send(redis, "celery", args=("a", 2), kwargs={"k": "v"}, headers={"eta": "2030-01-01T00:00:00+00:00", "retries": 2})
    beat_id = _send(redis, "celery", headers={"cm_sent_at": sent_at.isoformat()})
    app, _ = _make(runs=[TaskRun(task_id=task_id, task_name="demo.echo", status="queued", queue="celery")])
    messages = _client(app).get("/api/queues/celery/messages").json()["messages"]
    one, two = messages
    assert one["task_name"] == "demo.echo"
    assert one["args"] == "('a', 2)" and one["kwargs"] == "{'k': 'v'}"
    assert one["eta"] == "2030-01-01T00:00:00+00:00" and one["retries"] == 2
    assert one["run"] == {"status": "queued"}
    assert one["size_bytes"] > 0 and one["age_seconds"] is None
    assert "body" not in one
    assert two["task_id"] == beat_id and two["run"] is None
    assert 290 <= two["age_seconds"] <= 400


def test_age_comes_from_the_runs_beat_send_time(broker, redis):
    task_id = _send(redis, "celery")
    beat_sent = datetime.now(timezone.utc) - timedelta(seconds=120)
    app, _ = _make(runs=[TaskRun(task_id=task_id, task_name="demo.echo", status="queued", beat_sent_at=beat_sent)])
    message = _client(app).get("/api/queues/celery/messages").json()["messages"][0]
    assert 110 <= message["age_seconds"] <= 200


def test_unsupported_transport_says_so(broker):
    broker["driver"] = "filesystem"
    app, _ = _make()
    body = _client(app).get("/api/queues/celery/messages").json()
    assert body["supported"] is False and "filesystem" in body["reason"]


def test_rabbitmq_is_off_unless_turned_on(broker):
    broker["driver"] = "amqp"
    app, _ = _make()
    body = _client(app).get("/api/queues/celery/messages").json()
    assert body["supported"] is False and "queue_browser_amqp" in body["reason"]
    assert body["messages"] == []


def test_rabbitmq_peek_rejects_every_message_it_took(broker, monkeypatch):
    broker["driver"] = "amqp"
    pending = [_amqp_message(f"t{i}", tag=i) for i in range(30)]
    channel = MagicMock()
    channel.queue_declare.return_value = MagicMock(message_count=30)
    channel.basic_get.side_effect = lambda queue, no_ack: pending.pop(0) if pending else None
    conn = MagicMock()
    conn.transport = MagicMock(driver_type="amqp")
    conn.channel.return_value = channel
    monkeypatch.setattr(messages_mod, "_open_connection", lambda _app: conn)
    app, _ = _make(queue_browser_amqp=True)

    body = _client(app).get("/api/queues/celery/messages?limit=50&offset=5").json()
    assert body["approximate"] is True and body["side_effects"]
    assert body["total"] == 30
    # Never more than the cap taken off the queue, whatever was asked for.
    assert channel.basic_get.call_count == messages_mod.AMQP_PEEK_CAP
    assert all(call.kwargs["no_ack"] is False for call in channel.basic_get.call_args_list)
    assert [m["task_id"] for m in body["messages"]] == [f"t{i}" for i in range(5, 20)]
    rejected = sorted(call.args[0] for call in channel.basic_reject.call_args_list)
    assert rejected == list(range(20))
    assert all(call.kwargs["requeue"] is True for call in channel.basic_reject.call_args_list)


def test_rabbitmq_queue_not_declared_is_empty_but_a_refusal_is_503(broker, monkeypatch):
    class NotFound(Exception):
        code = 404

    channel = MagicMock()
    conn = MagicMock()
    conn.transport = MagicMock(driver_type="amqp")
    conn.channel.return_value = channel
    monkeypatch.setattr(messages_mod, "_open_connection", lambda _app: conn)
    app, _ = _make(queue_browser_amqp=True)
    client = _client(app)

    channel.queue_declare.side_effect = NotFound("NOT_FOUND - no queue 'celery'")
    body = client.get("/api/queues/celery/messages").json()
    assert body["total"] == 0 and body["messages"] == []
    channel.basic_get.assert_not_called()

    channel.queue_declare.side_effect = PermissionError("ACCESS_REFUSED")
    assert client.get("/api/queues/celery/messages").status_code == 503


def _amqp_message(task_id, tag):
    message = MagicMock()
    message.body = json.dumps([["x"], {}, {}]).encode()
    message.delivery_tag = tag
    message.properties = {
        "content_type": "application/json",
        "priority": 0,
        "application_headers": {"task": "demo.echo", "id": task_id, "argsrepr": "('x',)", "kwargsrepr": "{}"},
    }
    return message


def test_broker_down_is_503(monkeypatch):
    def refuse(_app):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(messages_mod, "_open_connection", refuse)
    app, _ = _make()
    response = _client(app).get("/api/queues/celery/messages")
    assert response.status_code == 503 and "refused" in response.json()["detail"]


def test_browse_requires_a_session():
    app, _ = _make()
    assert TestClient(app, base_url="https://testserver").get("/api/queues/celery/messages").status_code == 401


# --- arguments and roles -------------------------------------------------------


@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
def test_every_role_sees_arguments_as_on_task_runs(broker, redis, role):
    # docs/roles.md: viewers see task arguments on runs; browsing follows it.
    app, _ = _make()
    _send(redis, "celery", args=("secret",))
    body = _client(app, role).get("/api/queues/celery/messages").json()
    assert body["args_visible"] is True
    assert body["messages"][0]["args"] == "('secret',)"


@pytest.mark.parametrize("role", ["viewer", "admin"])
def test_arguments_can_be_turned_off_for_everyone(broker, redis, role):
    app, _ = _make(queue_browser_show_args=False)
    _send(redis, "celery", args=("secret",), kwargs={"token": "t0p"})
    response = _client(app, role).get("/api/queues/celery/messages")
    body = response.json()
    assert body["args_visible"] is False
    assert body["messages"][0]["args"] is None and body["messages"][0]["kwargs"] is None
    assert "secret" not in response.text and "t0p" not in response.text
    # The task itself is still identified.
    assert body["messages"][0]["task_name"] == "demo.echo"


def test_body_is_decoded_when_there_is_no_argsrepr():
    body = base64.b64encode(json.dumps([[1, "two"], {"x": 3}, {}]).encode()).decode()
    described = describe_message(
        {"task": "t", "id": "i"}, {}, body, content_type="application/json", body_encoding="base64",
        size_bytes=10, show_args=True,
    )
    assert described["args"] == '[1, "two"]' and described["kwargs"] == '{"x": 3}'


def test_protocol_1_messages_are_read_from_the_body():
    body = json.dumps({"task": "old.task", "id": "abc", "args": [1], "kwargs": {}, "retries": 1})
    described = describe_message(
        {}, {}, body, content_type="application/json", body_encoding=None, size_bytes=10, show_args=True
    )
    assert (described["task_name"], described["task_id"], described["retries"]) == ("old.task", "abc", 1)
    assert described["args"] == "[1]"


def test_pickle_and_huge_bodies_are_never_decoded():
    pickled = describe_message(
        {"task": "t", "id": "i"}, {}, b"\x80\x04...", content_type="application/x-python-serialize",
        body_encoding=None, size_bytes=5, show_args=True,
    )
    assert pickled["args"] is None and "isn't decoded" in pickled["args_note"]
    huge = describe_message(
        {"task": "t", "id": "i"}, {}, b"[" + b"1," * 70_000 + b"1]", content_type="application/json",
        body_encoding=None, size_bytes=140_000, show_args=True,
    )
    assert huge["args"] is None and "larger than" in huge["args_note"]


def test_long_arguments_are_cut():
    described = describe_message(
        {"task": "t", "id": "i", "argsrepr": "x" * 10_000, "kwargsrepr": "{}"}, {}, None,
        content_type="application/json", body_encoding=None, size_bytes=1, show_args=True,
    )
    assert len(described["args"]) == messages_mod.MAX_ARGS_CHARS + 1


# --- delete ----------------------------------------------------------------------


def test_delete_removes_exactly_that_message_marks_the_run_and_audits(broker, redis):
    keep_before = _send(redis, "celery")
    target = _send(redis, "celery")
    keep_after = _send(redis, "celery")
    app, factory = _make(runs=[TaskRun(task_id=target, task_name="demo.echo", status="queued", queue="celery")])

    response = _client(app).delete(f"/api/queues/celery/messages/{target}")
    assert response.status_code == 200, response.text
    assert response.json()["run_marked_revoked"] is True
    remaining = [json.loads(raw)["headers"]["id"] for raw in redis.lists["celery"]]
    assert remaining == [keep_after, keep_before]
    with factory() as session:
        assert session.query(TaskRun).filter_by(task_id=target).one().status == "revoked"
        audit = session.query(AuditLog).one()
        assert (audit.action, audit.username, audit.target_id) == ("delete_message", "admin", "celery")
        assert json.loads(audit.details)["task_id"] == target


def test_delete_finds_messages_in_priority_lists(broker, redis):
    app, _ = _make()
    target = _send(redis, "celery", priority=9)
    assert _client(app).delete(f"/api/queues/celery/messages/{target}").status_code == 200
    assert redis.lists[_key("celery", 9)] == []


def test_delete_of_a_missing_message_is_404_and_unaudited(broker, redis):
    app, factory = _make()
    _send(redis, "celery")
    response = _client(app).delete("/api/queues/celery/messages/nope")
    assert response.status_code == 404
    with factory() as session:
        assert session.query(AuditLog).count() == 0


def test_delete_when_a_worker_takes_it_after_we_found_it_is_409(broker, redis):
    app, factory = _make()
    target = _send(redis, "celery")
    # The worker pops it right after we located it and started watching.
    redis.after_watch = lambda: redis.rpop("celery")
    response = _client(app).delete(f"/api/queues/celery/messages/{target}")
    assert response.status_code == 409
    assert "already taken by a worker" in response.json()["detail"]
    with factory() as session:
        assert session.query(AuditLog).count() == 0


def test_a_push_during_delete_retries_and_still_removes_only_the_target(broker, redis):
    app, _ = _make()
    target = _send(redis, "celery")
    # A producer pushes between our check and EXEC: the transaction aborts
    # and is retried, and the new message survives.
    redis.before_exec = lambda: _send(redis, "celery")
    assert _client(app).delete(f"/api/queues/celery/messages/{target}").status_code == 200
    remaining = [json.loads(raw)["headers"]["id"] for raw in redis.lists["celery"]]
    assert len(remaining) == 1 and target not in remaining


def test_operator_and_viewer_cannot_delete_or_move(broker, redis):
    app, _ = _make()
    target = _send(redis, "celery")
    for role in ("viewer", "operator"):
        client = _client(app, role)
        assert client.delete(f"/api/queues/celery/messages/{target}").status_code == 403
        assert client.post(f"/api/queues/celery/messages/{target}/move", json={"target": "bulk"}).status_code == 403
    assert len(redis.lists["celery"]) == 1


def test_delete_on_rabbitmq_is_refused(broker):
    broker["driver"] = "amqp"
    app, _ = _make()
    response = _client(app).delete("/api/queues/celery/messages/abc")
    assert response.status_code == 409 and "Redis" in response.json()["detail"]


# --- move ------------------------------------------------------------------------


def test_move_keeps_priority_retargets_the_payload_and_audits(broker, redis):
    target = _send(redis, "celery", priority=6)
    app, factory = _make(runs=[TaskRun(task_id=target, task_name="demo.echo", status="queued", queue="celery")])
    response = _client(app).post(f"/api/queues/celery/messages/{target}/move", json={"target": "bulk"})
    assert response.status_code == 200, response.text
    assert redis.lists[_key("celery", 6)] == []
    (raw,) = redis.lists[_key("bulk", 6)]
    payload = json.loads(raw)
    assert payload["headers"]["id"] == target
    assert payload["properties"]["delivery_info"] == {"exchange": "", "routing_key": "bulk"}
    with factory() as session:
        assert session.query(TaskRun).filter_by(task_id=target).one().queue == "bulk"
        audit = session.query(AuditLog).one()
        assert audit.action == "move_message"
        assert json.loads(audit.details) == {"task_id": target, "task_name": "demo.echo", "from": "celery", "to": "bulk"}


def test_move_to_an_unknown_queue_is_refused_and_changes_nothing(broker, redis):
    app, factory = _make()
    target = _send(redis, "celery")
    response = _client(app).post(f"/api/queues/celery/messages/{target}/move", json={"target": "bulkk"})
    assert response.status_code == 400 and "known queue" in response.json()["detail"]
    assert len(redis.lists["celery"]) == 1 and "bulkk" not in redis.lists
    with factory() as session:
        assert session.query(AuditLog).count() == 0


def test_move_to_a_queue_seen_on_a_run_is_allowed(broker, redis):
    target = _send(redis, "celery")
    app, _ = _make(runs=[TaskRun(task_id="other", task_name="x", status="succeeded", queue="reports")])
    response = _client(app).post(f"/api/queues/celery/messages/{target}/move", json={"target": "reports"})
    assert response.status_code == 200
    assert len(redis.lists["reports"]) == 1


def test_move_onto_the_same_queue_is_refused(broker, redis):
    app, _ = _make()
    target = _send(redis, "celery")
    response = _client(app).post(f"/api/queues/celery/messages/{target}/move", json={"target": "celery"})
    assert response.status_code == 400


def test_move_when_a_worker_takes_it_first_never_duplicates(broker, redis):
    app, factory = _make()
    target = _send(redis, "celery")
    # Popped after our check, before EXEC: WATCH aborts the LREM+LPUSH as one.
    redis.before_exec = lambda: redis.rpop("celery")
    response = _client(app).post(f"/api/queues/celery/messages/{target}/move", json={"target": "bulk"})
    assert response.status_code == 409
    assert redis.lists.get("bulk", []) == []
    with factory() as session:
        assert session.query(AuditLog).count() == 0
