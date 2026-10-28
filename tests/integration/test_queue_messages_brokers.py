"""Browsing, deleting and moving waiting messages on a real Redis and RabbitMQ.

Skipped unless CM_TEST_REDIS_URL / CM_TEST_AMQP_URL point at a broker that
may be written to, e.g.

    docker run -d --rm --name cm-redis -p 6393:6379 redis:7-alpine
    CM_TEST_REDIS_URL=redis://localhost:6393/0 pytest tests/integration

Messages are published with `apply_async` while no worker runs, so they sit
on the broker exactly as a real backlog would. Queue names are unique per run.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from kombu import Queue

from celery_manager.control import messages as messages_mod
from celery_manager.control.messages import (
    MessageNotFound,
    delete_message,
    move_message,
    peek_messages,
)
from celery_manager.db.models import AuditLog, Base, TaskRun
from celery_manager.db.session import create_db_engine, get_session_factory

REDIS_URL = os.environ.get("CM_TEST_REDIS_URL")
AMQP_URL = os.environ.get("CM_TEST_AMQP_URL")


def _session_factory():
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return get_session_factory(engine)


def _app(broker: str, queues: list[str]):
    app = Celery(f"cm-peek-{uuid.uuid4().hex[:6]}", broker=broker)
    app.conf.task_queues = [Queue(name) for name in queues]
    app.conf.task_default_queue = queues[0]
    app.conf.broker_connection_retry_on_startup = False
    ran: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    @app.task(bind=True, name="cm.peek.record")
    def record(self, label, secret=None):
        with lock:
            ran.append((label, self.request.id, self.request.delivery_info.get("routing_key")))

    return app, record, ran


@pytest.mark.skipif(not REDIS_URL, reason="set CM_TEST_REDIS_URL to run against a real Redis")
def test_redis_browse_move_delete_then_a_worker_runs_only_the_right_tasks():
    suffix = uuid.uuid4().hex[:8]
    source, target = f"cm-peek-src-{suffix}", f"cm-peek-dst-{suffix}"
    app, record, ran = _app(REDIS_URL, [source, target])
    factory = _session_factory()

    # Lowest number runs first on Redis: 0 (the plain list), then 3, 6, 9.
    sent = {
        "low-1": record.apply_async(("low-1",), queue=source, priority=9),
        "plain-1": record.apply_async(("plain-1",), {"secret": "s3"}, queue=source),
        "high-1": record.apply_async(("high-1",), queue=source, priority=3),
        "plain-2": record.apply_async(("plain-2",), queue=source),
        "to-move": record.apply_async(("to-move",), queue=source, priority=3),
        "to-delete": record.apply_async(("to-delete",), queue=source),
    }
    ids = {label: result.id for label, result in sent.items()}

    with factory() as session:
        # A recorded run for the one we delete, as the events listener writes.
        session.add(TaskRun(task_id=ids["to-delete"], task_name="cm.peek.record", status="queued", queue=source))
        session.add(TaskRun(task_id=ids["to-move"], task_name="cm.peek.record", status="queued", queue=source))
        session.commit()

        page = peek_messages(app, session, source, limit=50)
        assert page["supported"] is True and page["approximate"] is False
        assert page["total"] == 6
        order = [m["task_id"] for m in page["messages"]]
        assert order == [
            ids["plain-1"], ids["plain-2"], ids["to-delete"],  # step 0, oldest first
            ids["high-1"], ids["to-move"],  # step 3
            ids["low-1"],  # step 9
        ]
        first = page["messages"][0]
        assert first["task_name"] == "cm.peek.record"
        assert first["args"] == "('plain-1',)" and "s3" in first["kwargs"]
        assert [m["position"] for m in page["messages"]] == list(range(6))
        assert page["messages"][2]["run"] == {"status": "queued"}

        # Paging across the priority lists.
        second = peek_messages(app, session, source, limit=2, offset=2)
        assert [m["task_id"] for m in second["messages"]] == [ids["to-delete"], ids["high-1"]]

        hidden = peek_messages(app, session, source, limit=1, show_args=False)
        assert hidden["messages"][0]["args"] is None and hidden["args_visible"] is False

        # Browsing took nothing.
        assert peek_messages(app, session, source)["total"] == 6

        move_message(app, session, source, ids["to-move"], target, username="admin")
        delete_message(app, session, source, ids["to-delete"], username="admin")
        with pytest.raises(MessageNotFound):
            delete_message(app, session, source, ids["to-delete"], username="admin")

        after = peek_messages(app, session, source)
        assert after["total"] == 4
        assert ids["to-move"] not in [m["task_id"] for m in after["messages"]]
        moved = peek_messages(app, session, target)["messages"]
        assert [m["task_id"] for m in moved] == [ids["to-move"]]
        assert moved[0]["priority"] == 3 and moved[0]["priority_step"] == 3

        assert session.query(TaskRun).filter_by(task_id=ids["to-delete"]).one().status == "revoked"
        assert session.query(TaskRun).filter_by(task_id=ids["to-move"]).one().queue == target
        actions = [row.action for row in session.query(AuditLog).order_by(AuditLog.id)]
        assert actions == ["move_message", "delete_message"]

    with start_worker(app, pool="solo", perform_ping_check=False, queues=[source, target], shutdown_timeout=10):
        deadline = time.monotonic() + 30
        while len(ran) < 5 and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(1)  # anything extra (the deleted one) would show up now

    by_label = {label: routing_key for label, _id, routing_key in ran}
    assert sorted(by_label) == ["high-1", "low-1", "plain-1", "plain-2", "to-move"]
    assert by_label["to-move"] == target
    assert all(by_label[label] == source for label in ("high-1", "low-1", "plain-1", "plain-2"))
    assert len(ran) == 5


@pytest.mark.skipif(not REDIS_URL, reason="set CM_TEST_REDIS_URL to run against a real Redis")
def test_redis_global_keyprefix_is_respected():
    suffix = uuid.uuid4().hex[:8]
    queue = f"cm-peek-prefixed-{suffix}"
    app, record, _ran = _app(REDIS_URL, [queue])
    app.conf.broker_transport_options = {"global_keyprefix": f"cmtest{suffix}:"}
    sent = record.apply_async(("x",), queue=queue, priority=6)
    with _session_factory()() as session:
        page = peek_messages(app, session, queue)
        assert [m["task_id"] for m in page["messages"]] == [sent.id]
        delete_message(app, session, queue, sent.id, username="admin")
        assert peek_messages(app, session, queue)["total"] == 0


@pytest.mark.skipif(not AMQP_URL, reason="set CM_TEST_AMQP_URL to run against a real RabbitMQ")
def test_rabbitmq_peek_is_opt_in_approximate_and_leaves_the_depth_alone():
    queue = f"cm-peek-amqp-{uuid.uuid4().hex[:8]}"
    app, record, _ran = _app(AMQP_URL, [queue])
    sent = [record.apply_async((f"m{i}",), queue=queue).id for i in range(25)]

    def depth():
        with app.connection_for_read() as conn:
            return conn.default_channel.queue_declare(queue=queue, passive=True).message_count

    try:
        assert depth() == 25
        with _session_factory()() as session:
            off = peek_messages(app, session, queue)
            assert off["supported"] is False and "queue_browser_amqp" in off["reason"]

            page = peek_messages(app, session, queue, limit=50, allow_amqp=True)
            assert page["supported"] is True and page["approximate"] is True
            assert page["side_effects"]
            assert page["total"] == 25
            # Capped, however many were asked for.
            assert len(page["messages"]) == messages_mod.AMQP_PEEK_CAP
            assert [m["task_id"] for m in page["messages"]] == sent[: messages_mod.AMQP_PEEK_CAP]
            assert page["messages"][0]["args"] == "('m0',)"

            # Everything went back.
            assert depth() == 25
            again = peek_messages(app, session, queue, limit=5, offset=3, allow_amqp=True)
            assert len(again["messages"]) == 5 and again["messages"][0]["position"] == 3
            assert depth() == 25
    finally:
        with app.connection_for_write() as conn:
            conn.default_channel.queue_delete(queue)
