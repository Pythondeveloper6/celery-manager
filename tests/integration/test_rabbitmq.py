"""RabbitMQ management and dead-letter queues against a real broker.

Skipped unless both are set, e.g.:

    docker run -d --rm --name cm-rabbit -p 5672:5672 -p 15672:15672 rabbitmq:3-management-alpine
    CM_TEST_AMQP_URL=amqp://guest:guest@localhost:5672//
    CM_TEST_RABBIT_MGMT_URL=http://localhost:15672
    (CM_TEST_RABBIT_MGMT_USER / CM_TEST_RABBIT_MGMT_PASSWORD, default guest/guest)

End to end: a work queue whose dead-letter exchange routes to a DLQ, real
Celery task messages rejected into it, browsed and requeued through
celery-manager, then run by a real worker.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from kombu import Exchange, Queue

from celery_manager.config import CeleryManagerConfig
from celery_manager.control import rabbitmq

AMQP_URL = os.environ.get("CM_TEST_AMQP_URL")
MGMT_URL = os.environ.get("CM_TEST_RABBIT_MGMT_URL")

pytestmark = pytest.mark.skipif(not (AMQP_URL and MGMT_URL),
                                reason="set CM_TEST_AMQP_URL and CM_TEST_RABBIT_MGMT_URL to run")


def _wait(predicate, timeout=20.0, step=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


@pytest.fixture
def setup():
    suffix = uuid.uuid4().hex[:8]
    work_name, dlq_name = f"cm-it-{suffix}", f"cm-it-{suffix}.failed"
    app = Celery(f"cm-it-{suffix}", broker=AMQP_URL)
    work_exchange = Exchange(work_name, type="direct")
    dlx = Exchange(f"{work_name}.dlx", type="direct")
    work = Queue(work_name, work_exchange, routing_key=work_name,
                 queue_arguments={"x-dead-letter-exchange": dlx.name, "x-dead-letter-routing-key": work_name})
    dlq = Queue(dlq_name, dlx, routing_key=work_name)
    app.conf.task_queues = [work]
    app.conf.task_default_queue = work_name
    app.conf.task_default_exchange = work_name
    app.conf.task_default_routing_key = work_name
    ran: list[int] = []
    lock = threading.Lock()

    @app.task(name="cm_it.record")
    def record(n):
        with lock:
            ran.append(n)
        return n

    with app.connection_for_write() as conn:
        channel = conn.channel()
        work(channel).declare()
        dlq(channel).declare()

    config = CeleryManagerConfig(
        celery_app=app, db_url="sqlite:///:memory:", secret_key="s",
        rabbitmq_management_url=MGMT_URL,
        rabbitmq_management_username=os.environ.get("CM_TEST_RABBIT_MGMT_USER", "guest"),
        rabbitmq_management_password=os.environ.get("CM_TEST_RABBIT_MGMT_PASSWORD", "guest"),
        rabbitmq_management_timeout_seconds=5.0,
    )
    yield {"app": app, "config": config, "work": work_name, "dlq": dlq_name, "task": record, "ran": ran}

    with app.connection_for_write() as conn:
        channel = conn.channel()
        for name in (work_name, dlq_name):
            channel.queue_delete(name)
        for exchange in (work_exchange.name, dlx.name):
            channel.exchange_delete(exchange)


def _reject_all(app, queue: str, expected: int) -> int:
    """Act as a worker that rejects every message without requeue, which
    makes RabbitMQ dead-letter it (reason `rejected`)."""
    rejected = 0
    with app.connection_for_write() as conn:
        channel = conn.channel()
        deadline = time.monotonic() + 10
        while rejected < expected and time.monotonic() < deadline:
            message = channel.basic_get(queue=queue, no_ack=False)
            if message is None:
                time.sleep(0.1)
                continue
            channel.basic_reject(message.delivery_info["delivery_tag"], requeue=False)
            rejected += 1
    return rejected


def _ready(config, name):
    # The management API refreshes queue stats every few seconds.
    return rabbitmq.fetch_queue(config, name)["messages_ready"]


class _Session:
    """Stands in for a database session: requeue only writes an audit row."""

    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    def commit(self):
        pass


def test_dead_lettered_celery_tasks_are_browsed_requeued_and_run(setup):
    app, config, work, dlq = setup["app"], setup["config"], setup["work"], setup["dlq"]
    sent = [setup["task"].apply_async(args=(n,)) for n in range(3)]
    assert _reject_all(app, work, 3) == 3

    # Detected through the work queue's dead-letter exchange and binding.
    dlqs = {q["name"]: q for q in rabbitmq.list_dlqs(config)["queues"]}
    assert dlqs[dlq]["matched_by"] == ["dead_letter_target"]
    assert dlqs[dlq]["dead_letter_sources"] == [work]

    browse = rabbitmq.browse_dlq(config, dlq, count=10)
    assert len(browse["messages"]) == 3
    first = browse["messages"][0]
    assert first["task_name"] == "cm_it.record"
    assert first["task_id"] in {r.id for r in sent}
    assert first["args"] is None and first["args_masked"] is True
    assert first["x_death"][0]["reason"] == "rejected"
    assert first["x_death"][0]["queue"] == work
    assert rabbitmq.browse_dlq(config, dlq, reveal_args=True)["messages"][0]["args"] in {"(0,)", "(1,)", "(2,)"}
    # Browsing put them back.
    assert _wait(lambda: _ready(config, dlq) == 3)

    # Requeue one selected message, then the rest.
    session = _Session()
    one = rabbitmq.requeue_dlq(config, session, dlq, "admin", keys=[first["key"]])
    assert one["moved"] == 1 and one["error"] is None
    rest = rabbitmq.requeue_dlq(config, session, dlq, "admin")
    assert rest["moved"] == 2 and rest["error"] is None
    assert [row.action for row in session.rows] == ["requeue_dead_letters"] * 2
    assert _wait(lambda: _ready(config, dlq) == 0 and _ready(config, work) == 3)

    # A real worker runs them.
    with start_worker(app, perform_ping_check=False, pool="solo", loglevel="WARNING", shutdown_timeout=30):
        assert _wait(lambda: sorted(setup["ran"]) == [0, 1, 2])


def test_loop_protection_and_force_on_a_real_broker(setup):
    app, config, work, dlq = setup["app"], setup["config"], setup["work"], setup["dlq"]
    setup["task"].apply_async(args=(7,))
    # Dead-letter it, requeue it, and repeat until it has died too often.
    for _ in range(rabbitmq.MAX_DEATHS):
        assert _reject_all(app, work, 1) == 1
        assert _wait(lambda: rabbitmq.requeue_dlq(config, _Session(), dlq, "admin")["moved"] == 1, timeout=5)
    assert _reject_all(app, work, 1) == 1

    refused = rabbitmq.requeue_dlq(config, _Session(), dlq, "admin")
    assert refused["moved"] == 0
    assert "dead-lettered" in refused["refused"][0]["reason"]

    forced = rabbitmq.requeue_dlq(config, _Session(), dlq, "admin", force=True)
    assert forced["moved"] == 1
    with start_worker(app, perform_ping_check=False, pool="solo", loglevel="WARNING", shutdown_timeout=30):
        assert _wait(lambda: setup["ran"] == [7])


def test_unroutable_requeue_leaves_the_message_in_the_dlq(setup):
    app, config, work, dlq = setup["app"], setup["config"], setup["work"], setup["dlq"]
    setup["task"].apply_async(args=(1,))
    assert _reject_all(app, work, 1) == 1
    # The original queue is gone: the copy would be dropped, so the original
    # must stay. (Still a DLQ by name pattern, now nothing dead-letters to it.)
    with app.connection_for_write() as conn:
        conn.channel().queue_delete(work)
    config.rabbitmq_dlq_patterns = ["*.failed"]
    result = rabbitmq.requeue_dlq(config, _Session(), dlq, "admin")
    assert result["moved"] == 0
    assert "could not route" in result["error"]
    assert _wait(lambda: _ready(config, dlq) == 1)


def test_overview_and_queue_details_from_the_real_management_api(setup):
    config, work = setup["config"], setup["work"]
    overview = rabbitmq.overview(config)
    assert overview["configured"] is True
    assert overview["nodes"] and overview["nodes"][0]["mem_limit"] > 0
    assert overview["alarms"]["ok"] is True
    details = rabbitmq.fetch_queue(config, work)
    assert details["type"] == "classic"
    assert details["dead_letter_exchange"] == f"{work}.dlx"
