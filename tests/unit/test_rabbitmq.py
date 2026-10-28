"""RabbitMQ: the management API client, the Queues page merge, dead-letter
queue detection, browsing, requeue, the rabbitmq_alarm alert and metrics.

The management API is a real HTTP server (stdlib, on a thread) answering
canned JSON, so the client's actual urllib path is exercised: auth header,
timeouts, redirects, error statuses. Requeue runs against a fake AMQP
channel that records the order of calls, which is the property that
matters (publish confirmed before ack).
"""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.alerts.evaluator import evaluate_once
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import alerts as alert_control
from celery_manager.control import rabbitmq
from celery_manager.db.models import AlertIncident, AuditLog
from celery_manager.metrics import compute_snapshot, render

PASSWORD = "s3cret-mgmt-pw"
AMQP = "amqp://guest:guest@rabbit.invalid:5672//"


# --- a fake management API ---------------------------------------------------------------


class FakeManagement:
    """Routes `(method, path)` to `(status, body)` or a callable taking the
    request and returning one. Records every request."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[dict] = []
        self.delay = 0.0
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self, method):
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                request = {"method": method, "path": unquote(parts.path), "raw_path": parts.path,
                           "query": parse_qs(parts.query), "headers": dict(self.headers), "body": body}
                fake.requests.append(request)
                if fake.delay:
                    time.sleep(fake.delay)
                answer = fake.routes.get((method, request["path"]), (404, {"error": "Object Not Found"}))
                if callable(answer):
                    answer = answer(request)
                status, payload, *extra = answer
                headers = extra[0] if extra else {}
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        return Handler

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mgmt():
    fake = FakeManagement()
    yield fake
    fake.close()


def _config(mgmt=None, broker=AMQP, **kwargs):
    return CeleryManagerConfig(
        celery_app=Celery("t", broker=broker),
        db_url="sqlite:///:memory:",
        secret_key="test-secret",
        rabbitmq_management_url=mgmt.url if mgmt else None,
        rabbitmq_management_username="monitor" if mgmt else None,
        rabbitmq_management_password=PASSWORD if mgmt else None,
        **kwargs,
    )


NODE = {
    "name": "rabbit@a", "running": True, "mem_used": 100, "mem_limit": 1000, "mem_alarm": False,
    "disk_free": 5000, "disk_free_limit": 50, "disk_free_alarm": False, "fd_used": 40, "fd_total": 1024,
    "sockets_used": 2, "sockets_total": 900, "uptime": 1,
}


def _queue(name, messages=0, **fields):
    return {"name": name, "type": "classic", "arguments": {}, "consumers": 0, "messages": messages,
            "messages_ready": messages, "messages_unacknowledged": 0, "state": "running", **fields}


# A work queue dead-lettering through an exchange, one through the default
# exchange, one whose DLX has nothing bound, and a pattern-named DLQ.
QUEUES = [
    _queue("work", arguments={"x-dead-letter-exchange": "dlx", "x-dead-letter-routing-key": "work"},
           consumers=3, messages_unacknowledged=7, idle_since="2026-10-28T08:00:00Z",
           message_stats={"publish_details": {"rate": 2.5}, "deliver_get_details": {"rate": 1.5}}),
    _queue("work.failed", messages=4),
    _queue("orders", arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": "orders.parked",
                                "x-queue-type": "quorum", "x-max-priority": 10}, type=None),
    _queue("orders.parked", messages=1),
    _queue("lost", effective_policy_definition={"dead-letter-exchange": "nowhere-bound"}),
    _queue("jobs.dlq", messages=2),
    _queue("celery"),
]


def _route_cluster(mgmt, queues=QUEUES, nodes=(NODE,), alarms=(200, {"status": "ok"})):
    mgmt.routes[("GET", "/api/queues//")] = (200, list(queues))
    mgmt.routes[("GET", "/api/nodes")] = (200, list(nodes))
    mgmt.routes[("GET", "/api/overview")] = (200, {
        "rabbitmq_version": "3.13.7", "management_version": "3.13.7", "cluster_name": "c1",
        "queue_totals": {"messages": 7, "messages_ready": 7, "messages_unacknowledged": 0},
        "message_stats": {"publish_details": {"rate": 4.0}},
    })
    mgmt.routes[("GET", "/api/health/checks/alarms")] = alarms
    mgmt.routes[("GET", "/api/exchanges///dlx")] = (200, {"name": "dlx", "type": "direct"})
    mgmt.routes[("GET", "/api/exchanges///dlx/bindings/source")] = (200, [
        {"destination": "work.failed", "destination_type": "queue", "routing_key": "work"},
        {"destination": "other", "destination_type": "queue", "routing_key": "something-else"},
    ])
    mgmt.routes[("GET", "/api/exchanges///nowhere-bound")] = (200, {"name": "nowhere-bound", "type": "fanout"})
    mgmt.routes[("GET", "/api/exchanges///nowhere-bound/bindings/source")] = (200, [])


def _client(config, role="admin"):
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, role, "pw", role=role)
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    assert client.post("/api/auth/login", json={"username": role, "password": "pw"}).status_code == 200
    return client, session_factory


# --- not configured -------------------------------------------------------------------------


def test_without_a_management_url_every_route_says_not_configured():
    client, _ = _client(_config())
    for path in ("/api/rabbitmq/overview", "/api/rabbitmq/dlqs", "/api/rabbitmq/dlqs/x.dlq/messages"):
        body = client.get(path).json()
        assert body == {"configured": False, "reason": "rabbitmq_management_url is not set"}
    response = client.post("/api/rabbitmq/dlqs/x.dlq/requeue", json={})
    assert response.status_code == 409
    assert "rabbitmq_management_url is not set" in response.json()["detail"]


def test_a_redis_broker_is_not_rabbitmq_even_with_a_url(mgmt):
    client, _ = _client(_config(mgmt, broker="redis://localhost:6379/0"))
    body = client.get("/api/rabbitmq/overview").json()
    assert body == {"configured": False, "reason": "the broker is redis, not RabbitMQ"}
    assert mgmt.requests == []  # never asked


def test_queues_listing_keeps_its_shape_without_management():
    listing = {"broker": {"transport": "redis"}, "queues": [{"name": "celery", "depth": 1}]}
    rabbitmq.annotate_queues(_config(), listing)
    assert listing["queues"] == [{"name": "celery", "depth": 1}]
    assert listing["rabbitmq"]["configured"] is False
    assert listing["rabbitmq"]["is_rabbitmq"] is False


# --- the client: auth, errors, deadline -------------------------------------------------------


def test_overview_reports_nodes_alarms_and_sends_basic_auth(mgmt):
    _route_cluster(mgmt)
    body = rabbitmq.overview(_config(mgmt))
    assert body["configured"] is True
    assert body["rabbitmq_version"] == "3.13.7"
    assert body["vhost"] == "/"
    assert body["publish_rate"] == 4.0
    assert body["nodes"][0]["mem_limit"] == 1000
    assert body["alarms"] == {"ok": True, "reason": None, "nodes": []}
    expected = "Basic " + base64.b64encode(f"monitor:{PASSWORD}".encode()).decode()
    assert {r["headers"]["Authorization"] for r in mgmt.requests} == {expected}
    nodes_request = next(r for r in mgmt.requests if r["path"] == "/api/nodes")
    assert "mem_alarm" in nodes_request["query"]["columns"][0]  # asks for columns, not whole objects


def test_alarm_health_check_503_is_an_answer_not_an_error(mgmt):
    alarmed = {**NODE, "mem_alarm": True}
    _route_cluster(mgmt, nodes=[alarmed],
                   alarms=(503, {"status": "failed", "reason": "resource alarm(s) in effect"}))
    body = rabbitmq.overview(_config(mgmt))
    assert body["alarms"] == {"ok": False, "reason": "resource alarm(s) in effect", "nodes": ["rabbit@a"]}


def test_401_says_check_the_credentials_and_never_echoes_them(mgmt):
    mgmt.routes[("GET", "/api/overview")] = (401, {"error": "not_authorised"})
    client, _ = _client(_config(mgmt))
    response = client.get("/api/rabbitmq/overview")
    assert response.status_code == 502
    assert "check the management credentials" in response.json()["detail"]
    assert PASSWORD not in response.text


def test_403_names_the_permissions_needed(mgmt):
    mgmt.routes[("GET", "/api/overview")] = (403, {"error": "forbidden"})
    with pytest.raises(rabbitmq.RabbitMQError, match="monitoring tag"):
        rabbitmq.overview(_config(mgmt))


def test_a_slow_management_api_times_out_within_the_budget(mgmt):
    _route_cluster(mgmt)
    mgmt.delay = 2.0
    started = time.monotonic()
    with pytest.raises(rabbitmq.RabbitMQError, match="did not answer within 0.3s"):
        rabbitmq.overview(_config(mgmt, rabbitmq_management_timeout_seconds=0.3))
    assert time.monotonic() - started < 1.5


def test_unreachable_management_api_is_a_clear_error():
    config = _config()
    config.rabbitmq_management_url = "http://127.0.0.1:1"
    with pytest.raises(rabbitmq.RabbitMQError, match="could not reach the management API"):
        rabbitmq.overview(config)


def test_redirects_are_not_followed(mgmt):
    mgmt.routes[("GET", "/api/overview")] = (302, {}, {"Location": "http://elsewhere.invalid/api/overview"})
    with pytest.raises(rabbitmq.RabbitMQError, match="redirected to http://elsewhere.invalid"):
        rabbitmq.overview(_config(mgmt))
    assert [r["path"] for r in mgmt.requests] == ["/api/overview"]


def test_non_json_answer_is_reported(mgmt):
    client = rabbitmq.ManagementClient(mgmt.url, "u", "p")
    client._opener = MagicMock()
    client._opener.open.return_value.__enter__.return_value.read.return_value = b"<html>"
    with pytest.raises(rabbitmq.RabbitMQError, match="isn't JSON"):
        client.get("/api/overview")


def test_credentials_in_the_url_are_moved_to_the_header(mgmt):
    _route_cluster(mgmt)
    host = mgmt.url.removeprefix("http://")
    client = rabbitmq.ManagementClient(f"http://u%40x:p%3Aw@{host}/", None, None)
    assert "@" not in client.base and "p:w" not in client.base
    client.get("/api/nodes")
    expected = "Basic " + base64.b64encode(b"u@x:p:w").decode()
    assert mgmt.requests[-1]["headers"]["Authorization"] == expected


def test_password_is_not_in_the_config_repr(mgmt):
    assert PASSWORD not in repr(_config(mgmt))


def test_vhost_comes_from_the_broker_url(mgmt):
    mgmt.routes[("GET", "/api/queues/prod/work")] = (200, _queue("work", type="quorum"))
    config = _config(mgmt, broker="amqp://guest:guest@rabbit.invalid:5672/prod")
    assert rabbitmq.fetch_queue(config, "work")["type"] == "quorum"
    assert mgmt.requests[-1]["raw_path"] == "/api/queues/prod/work"


def test_default_vhost_is_encoded(mgmt):
    _route_cluster(mgmt)
    rabbitmq.list_dlqs(_config(mgmt))
    assert mgmt.requests[0]["raw_path"] == "/api/queues/%2F"


# --- the Queues page --------------------------------------------------------------------------


def test_queues_route_merges_management_details(mgmt, monkeypatch):
    _route_cluster(mgmt)
    from celery_manager.api.routes import queues as queue_routes

    monkeypatch.setattr(queue_routes, "list_queues", lambda *a, **k: {
        "broker": {"transport": "amqp", "error": None},
        "queues": [{"name": "work", "depth": 3}, {"name": "orders", "depth": 0}, {"name": "unknown", "depth": None}],
        "recorded_queued_unassigned": 0,
    })
    client, _ = _client(_config(mgmt), role="viewer")
    body = client.get("/api/queues").json()
    assert body["rabbitmq"] == {"configured": True, "reason": None, "error": None, "is_rabbitmq": True}
    by_name = {q["name"]: q for q in body["queues"]}
    work = by_name["work"]["rabbitmq"]
    assert work["consumers"] == 3
    assert work["messages_unacknowledged"] == 7
    assert work["publish_rate"] == 2.5
    assert work["deliver_rate"] == 1.5
    assert work["dead_letter_exchange"] == "dlx"
    assert work["dead_letter_routing_key"] == "work"
    assert work["idle_since"] == "2026-10-28T08:00:00Z"
    orders = by_name["orders"]["rabbitmq"]
    assert orders["type"] == "quorum"  # from x-queue-type when `type` is missing
    assert orders["max_priority"] == 10
    assert orders["dead_letter_exchange"] == ""
    assert by_name["unknown"]["rabbitmq"] is None
    queue_request = next(r for r in mgmt.requests if r["path"] == "/api/queues//")
    assert "messages_unacknowledged" in queue_request["query"]["columns"][0]


def test_queues_route_survives_a_failing_management_api(mgmt, monkeypatch):
    mgmt.routes[("GET", "/api/queues//")] = (500, {"error": "boom"})
    from celery_manager.api.routes import queues as queue_routes

    monkeypatch.setattr(queue_routes, "list_queues", lambda *a, **k: {
        "broker": {"transport": "amqp", "error": None}, "queues": [{"name": "work", "depth": 3}],
        "recorded_queued_unassigned": 0,
    })
    client, _ = _client(_config(mgmt), role="viewer")
    response = client.get("/api/queues")
    assert response.status_code == 200
    body = response.json()
    assert "500" in body["rabbitmq"]["error"]
    assert body["queues"][0]["depth"] == 3
    assert body["queues"][0]["rabbitmq"] is None


# --- dead-letter queue detection ------------------------------------------------------------------


def test_dlqs_are_found_by_dead_letter_target_and_by_name(mgmt):
    _route_cluster(mgmt)
    body = rabbitmq.list_dlqs(_config(mgmt))
    by_name = {q["name"]: q for q in body["queues"]}
    assert set(by_name) == {"work.failed", "orders.parked", "jobs.dlq"}
    assert by_name["work.failed"]["matched_by"] == ["dead_letter_target"]
    assert by_name["work.failed"]["dead_letter_sources"] == ["work"]
    assert by_name["work.failed"]["messages"] == 4
    assert by_name["orders.parked"]["dead_letter_sources"] == ["orders"]
    assert by_name["jobs.dlq"]["matched_by"] == ["name_pattern"]
    # "other" is bound to the DLX with a different key, so it isn't a target.
    assert "other" not in by_name
    # A policy-set DLX with nothing bound: dead letters are dropped, say so.
    assert any("lost" in w and "dropped" in w for w in body["warnings"])


def test_custom_patterns_replace_the_defaults(mgmt):
    _route_cluster(mgmt)
    body = rabbitmq.list_dlqs(_config(mgmt, rabbitmq_dlq_patterns=["cel*"]))
    names = {q["name"] for q in body["queues"]}
    assert "celery" in names and "jobs.dlq" not in names


@pytest.mark.parametrize(("pattern", "key", "expected"), [
    ("a.*", "a.b", True), ("a.*", "a.b.c", False), ("a.#", "a.b.c", True), ("#", "x", True),
    ("a.#.z", "a.z", True), ("*.b", "a.c", False),
])
def test_topic_binding_matching(pattern, key, expected):
    assert rabbitmq._topic_matches(pattern, key) is expected


# --- browsing --------------------------------------------------------------------------------


def _dead_message(task_id="t-1", count=1, argsrepr="('secret-token',)", encoding="string"):
    body = json.dumps([["secret-token"], {"pin": 1234}, {}])
    payload = base64.b64encode(body.encode()).decode() if encoding == "base64" else body
    return {
        "payload": payload, "payload_encoding": encoding, "payload_bytes": len(body), "redelivered": False,
        "exchange": "dlx", "routing_key": "work", "message_count": 0,
        "properties": {
            "content_type": "application/json",
            "headers": {
                "task": "billing.charge", "id": task_id, "argsrepr": argsrepr, "kwargsrepr": "{'pin': 1234}",
                "x-first-death-reason": "rejected", "x-first-death-queue": "work",
                "x-death": [{"reason": "rejected", "queue": "work", "exchange": "celery", "routing-keys": ["work"],
                             "count": count, "time": 1761638400}],
            },
        },
    }


def test_browse_masks_arguments_by_default_and_shows_x_death(mgmt):
    _route_cluster(mgmt)
    mgmt.routes[("POST", "/api/queues///work.failed/get")] = (200, [_dead_message()])
    client, _ = _client(_config(mgmt), role="viewer")
    response = client.get("/api/rabbitmq/dlqs/work.failed/messages?count=500")
    assert response.status_code == 200
    body = response.json()
    assert body["approximate"] is True and body["limit"] == 20
    get_request = next(r for r in mgmt.requests if r["method"] == "POST")
    assert get_request["body"]["ackmode"] == "ack_requeue_true"  # puts them back
    assert get_request["body"]["count"] == 20  # capped
    message = body["messages"][0]
    assert "secret-token" not in response.text and "1234" not in response.text
    assert message["args"] is None and message["args_masked"] is True
    assert message["task_name"] == "billing.charge" and message["task_id"] == "t-1"
    assert message["key"] == "t-1"
    assert message["x_death"] == [{"reason": "rejected", "queue": "work", "exchange": "celery",
                                   "routing_keys": ["work"], "count": 1, "time": "2025-10-28T08:00:00+00:00"}]
    assert message["first_death_reason"] == "rejected"


def test_browse_reveals_arguments_when_asked(mgmt):
    _route_cluster(mgmt)
    mgmt.routes[("POST", "/api/queues///work.failed/get")] = (200, [_dead_message()])
    client, _ = _client(_config(mgmt), role="viewer")
    message = client.get("/api/rabbitmq/dlqs/work.failed/messages?reveal_args=true").json()["messages"][0]
    # Celery's own argsrepr wins over the raw body: it is what the app chose to show.
    assert message["args"] == "('secret-token',)"
    assert message["kwargs"] == "{'pin': 1234}"


def test_browse_reads_the_body_when_there_is_no_argsrepr(mgmt):
    item = _dead_message(encoding="base64")
    del item["properties"]["headers"]["argsrepr"]
    del item["properties"]["headers"]["kwargsrepr"]
    message = rabbitmq.describe_message(item, reveal_args=True)
    assert message["args"] == '["secret-token"]'
    assert json.loads(message["kwargs"]) == {"pin": 1234}


def test_browse_refuses_a_queue_that_is_not_a_dlq(mgmt):
    _route_cluster(mgmt)
    client, _ = _client(_config(mgmt), role="viewer")
    response = client.get("/api/rabbitmq/dlqs/work/messages")
    assert response.status_code == 404
    assert "not a dead-letter queue" in response.json()["detail"]
    assert not any(r["method"] == "POST" for r in mgmt.requests)


# --- requeue: the AMQP side -----------------------------------------------------------------


class FakeAmqpMessage:
    def __init__(self, tag, headers, body=b"{}", message_id=None):
        self.delivery_info = {"delivery_tag": tag}
        self.body = body
        self.properties = {"content_type": "application/json", "delivery_mode": 2,
                           "application_headers": headers}
        if message_id:
            self.properties["message_id"] = message_id


class FakeChannel:
    """An AMQP channel over a list of messages. `calls` records the order of
    everything that matters; `fail_publish_on` / `return_on` make the n-th
    publish (1-based) raise or come back unroutable."""

    def __init__(self, messages, fail_publish_on=None, return_on=None):
        self.queue = list(messages)
        self.calls: list[tuple] = []
        self.published: list = []
        self.events = {"basic_return": set()}
        self.fail_publish_on = fail_publish_on
        self.return_on = return_on
        self.publishes = 0

    def confirm_select(self):
        self.calls.append(("confirm_select",))

    def basic_get(self, queue, no_ack):
        assert no_ack is False
        message = self.queue.pop(0) if self.queue else None
        self.calls.append(("get", message.delivery_info["delivery_tag"] if message else None))
        return message

    def basic_publish_confirm(self, message, exchange, routing_key, mandatory, confirm_timeout):
        assert mandatory is True
        self.publishes += 1
        self.calls.append(("publish", exchange, routing_key))
        if self.publishes == self.fail_publish_on:
            raise ConnectionError("nack")
        if self.publishes == self.return_on:
            for callback in list(self.events["basic_return"]):
                callback(Exception("NO_ROUTE"), exchange, routing_key, message)
        self.published.append(message)

    def basic_ack(self, tag):
        self.calls.append(("ack", tag))

    def basic_reject(self, tag, requeue):
        assert requeue is True
        self.calls.append(("reject", tag))


def _x_death(count=1, exchange="celery", key="work"):
    return [{"reason": "rejected", "queue": "work", "exchange": exchange, "routing-keys": [key], "count": count,
             "time": datetime(2026, 10, 28, tzinfo=timezone.utc)}]


def _messages(n, count=1):
    return [FakeAmqpMessage(i, {"id": f"t-{i}", "task": "a.b", "x-death": _x_death(count)}) for i in range(1, n + 1)]


def test_requeue_publishes_and_confirms_before_acking_each_message():
    channel = FakeChannel(_messages(2))
    result = rabbitmq.requeue_on_channel(channel, "work.failed", keys=None, limit=10, force=False)
    assert result["moved"] == 2 and result["error"] is None
    assert channel.calls == [
        ("confirm_select",),
        ("get", 1), ("publish", "celery", "work"), ("ack", 1),
        ("get", 2), ("publish", "celery", "work"), ("ack", 2),
        ("get", None),
    ]
    headers = channel.published[0].properties["application_headers"]
    assert headers[rabbitmq.REQUEUE_HEADER] == 1
    assert headers["x-death"]  # kept, see the docstring
    assert channel.published[0].properties["delivery_mode"] == 2


def test_a_failed_publish_stops_and_leaves_the_rest_in_the_dlq():
    channel = FakeChannel(_messages(3), fail_publish_on=2)
    result = rabbitmq.requeue_on_channel(channel, "work.failed", keys=None, limit=10, force=False)
    assert result["moved"] == 1
    assert "nack" in result["error"]
    # Message 2 was never acked (handed back); message 3 was never taken.
    assert ("ack", 2) not in channel.calls
    assert ("reject", 2) in channel.calls
    assert ("get", 3) not in channel.calls
    assert channel.calls.index(("ack", 1)) < channel.calls.index(("publish", "celery", "work"), 3)


def test_an_unroutable_message_is_not_acked():
    channel = FakeChannel(_messages(2), return_on=1)
    result = rabbitmq.requeue_on_channel(channel, "work.failed", keys=None, limit=10, force=False)
    assert result["moved"] == 0
    assert "could not route" in result["error"]
    assert ("ack", 1) not in channel.calls and ("reject", 1) in channel.calls


def test_loop_protection_refuses_messages_that_died_too_often_unless_forced():
    channel = FakeChannel(_messages(1, count=rabbitmq.MAX_DEATHS + 1))
    result = rabbitmq.requeue_on_channel(channel, "work.failed", keys=None, limit=10, force=False)
    assert result["moved"] == 0
    assert "dead-lettered 4 times" in result["refused"][0]["reason"]
    assert ("reject", 1) in channel.calls and ("ack", 1) not in channel.calls

    channel = FakeChannel(_messages(1, count=rabbitmq.MAX_DEATHS + 1))
    assert rabbitmq.requeue_on_channel(channel, "work.failed", keys=None, limit=10, force=True)["moved"] == 1


def test_loop_protection_counts_our_own_requeues():
    message = FakeAmqpMessage(1, {"id": "t-1", "x-death": _x_death(1), rabbitmq.REQUEUE_HEADER: 3})
    result = rabbitmq.requeue_on_channel(FakeChannel([message]), "q", keys=None, limit=10, force=False)
    assert result["moved"] == 0 and result["refused"]


def test_a_message_without_x_death_is_refused():
    message = FakeAmqpMessage(1, {"id": "t-1"})
    result = rabbitmq.requeue_on_channel(FakeChannel([message]), "q", keys=None, limit=10, force=True)
    assert result["moved"] == 0
    assert "no x-death" in result["refused"][0]["reason"]


def test_selected_keys_only_and_the_others_are_handed_back():
    channel = FakeChannel(_messages(3))
    result = rabbitmq.requeue_on_channel(channel, "q", keys={"t-2", "t-9"}, limit=10, force=False)
    assert [m["key"] for m in result["moved_messages"]] == ["t-2"]
    assert result["not_found"] == ["t-9"]
    assert ("reject", 1) in channel.calls and ("reject", 3) in channel.calls
    assert ("ack", 1) not in channel.calls


def test_limit_stops_after_n_messages():
    channel = FakeChannel(_messages(5))
    result = rabbitmq.requeue_on_channel(channel, "q", keys=None, limit=2, force=False)
    assert result["moved"] == 2
    assert ("get", 3) not in channel.calls


def test_the_time_budget_stops_between_messages():
    ticks = iter([0.0, 0.0, 100.0, 100.0])
    channel = FakeChannel(_messages(3))
    result = rabbitmq.requeue_on_channel(channel, "q", keys=None, limit=10, force=False,
                                         budget_seconds=30, clock=lambda: next(ticks))
    assert result["moved"] == 1
    assert "longer than 30s" in result["stopped"]


# --- requeue: the route -----------------------------------------------------------------------


def test_requeue_route_is_audited(mgmt, monkeypatch):
    _route_cluster(mgmt)
    channel = FakeChannel(_messages(2))
    conn = MagicMock()
    conn.channel.return_value = channel
    monkeypatch.setattr(rabbitmq, "_open_amqp", lambda *a: conn)
    client, session_factory = _client(_config(mgmt))
    response = client.post("/api/rabbitmq/dlqs/work.failed/requeue", json={"limit": 5})
    assert response.status_code == 200, response.text
    assert response.json()["moved"] == 2
    conn.release.assert_called_once()
    with session_factory() as session:
        entry = session.query(AuditLog).filter_by(action="requeue_dead_letters").one()
        assert entry.username == "admin" and entry.target_id == "work.failed"
        assert json.loads(entry.details)["task_ids"] == ["t-1", "t-2"]


def test_requeue_route_refuses_a_queue_that_is_not_a_dlq(mgmt):
    _route_cluster(mgmt)
    client, session_factory = _client(_config(mgmt))
    response = client.post("/api/rabbitmq/dlqs/work/requeue", json={})
    assert response.status_code == 404
    with session_factory() as session:
        assert session.query(AuditLog).filter_by(action="requeue_dead_letters").count() == 0


def test_requeue_route_reports_a_broker_it_cannot_reach(mgmt, monkeypatch):
    _route_cluster(mgmt)

    def refuse(*args):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(rabbitmq, "_open_amqp", refuse)
    client, _ = _client(_config(mgmt))
    response = client.post("/api/rabbitmq/dlqs/work.failed/requeue", json={})
    assert response.status_code == 502
    assert "could not connect to the broker" in response.json()["detail"]


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_requeue_needs_admin(mgmt, role):
    _route_cluster(mgmt)
    client, _ = _client(_config(mgmt), role=role)
    response = client.post("/api/rabbitmq/dlqs/work.failed/requeue", json={})
    assert response.status_code == 403
    assert not any(r["path"].startswith("/api/queues") for r in mgmt.requests)


def test_requeue_validates_keys(mgmt):
    client, _ = _client(_config(mgmt))
    assert client.post("/api/rabbitmq/dlqs/work.failed/requeue", json={"keys": []}).status_code == 422
    assert client.post("/api/rabbitmq/dlqs/work.failed/requeue", json={"limit": 0}).status_code == 422


# --- alerts -------------------------------------------------------------------------------------


@pytest.fixture
def alert_env(tmp_path):
    from celery_manager.db.session import get_session_factory, init_db

    config = CeleryManagerConfig(celery_app=Celery("t", broker=AMQP), db_url=f"sqlite:///{tmp_path / 'cm.db'}",
                                 secret_key="s", rabbitmq_management_url="http://rabbit.invalid:15672")
    engine = init_db(config)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        alert_control.create_rule(session, "admin", {"name": "rabbit", "kind": "rabbitmq_alarm", "target": "*"})
    yield config, session_factory
    engine.dispose()


def _incidents(session_factory):
    with session_factory() as session:
        return [(i.key, i.state) for i in session.query(AlertIncident).order_by(AlertIncident.id)]


def test_rabbitmq_alarm_fires_per_node_and_resolves(alert_env):
    config, session_factory = alert_env
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    nodes = [{**NODE, "name": "rabbit@a", "mem_alarm": True}, {**NODE, "name": "rabbit@b"}]
    evaluate_once(session_factory, config.celery_app, config, now, senders={}, rabbitmq_nodes_fn=lambda c: nodes)
    assert _incidents(session_factory) == [("rabbit@a", "firing")]
    with session_factory() as session:
        details = json.loads(session.query(AlertIncident).one().details)
        assert details["alarms"] == ["memory"]

    nodes[0]["mem_alarm"] = False
    evaluate_once(session_factory, config.celery_app, config, now + timedelta(minutes=1), senders={},
                  rabbitmq_nodes_fn=lambda c: nodes)
    assert _incidents(session_factory) == [("rabbit@a", "resolved")]


def test_rabbitmq_alarm_that_cannot_be_checked_neither_opens_nor_resolves(alert_env):
    config, session_factory = alert_env
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    evaluate_once(session_factory, config.celery_app, config, now, senders={},
                  rabbitmq_nodes_fn=lambda c: [{**NODE, "disk_free_alarm": True}])

    def down(config):
        raise rabbitmq.RabbitMQError("could not reach the management API: refused")

    evaluate_once(session_factory, config.celery_app, config, now + timedelta(minutes=1), senders={},
                  rabbitmq_nodes_fn=down)
    assert _incidents(session_factory) == [("rabbit@a", "firing")]
    with session_factory() as session:
        rule = session.query(alert_control.AlertRule).one()
        assert "could not reach the management API" in rule.last_check_error


def test_rabbitmq_alarm_rule_takes_no_parameters(alert_env):
    _, session_factory = alert_env
    with session_factory() as session, pytest.raises(ValueError, match="unknown parameter"):
        alert_control.create_rule(session, "admin", {"name": "x", "kind": "rabbitmq_alarm", "params": {"depth": 1}})


# --- metrics ------------------------------------------------------------------------------------


def test_metrics_include_rabbitmq_queue_and_node_families(mgmt, monkeypatch):
    from celery_manager import metrics as metrics_module

    _route_cluster(mgmt, nodes=[{**NODE, "disk_free_alarm": True}])
    monkeypatch.setattr(metrics_module, "list_queues", lambda *a, **k: {"broker": {"error": None}, "queues": []})
    monkeypatch.setattr(metrics_module, "_workers_family",
                        lambda *a, **k: metrics_module.Family("celery_manager_workers_online", "w", [({}, 1)]))
    config = _config(mgmt)
    _, session_factory = create_app(config)
    text = render(compute_snapshot(config, session_factory))
    assert 'celery_manager_rabbitmq_queue_unacked{queue="work"} 7' in text
    assert 'celery_manager_rabbitmq_queue_consumers{queue="work"} 3' in text
    assert 'celery_manager_rabbitmq_queue_publish_rate{queue="work"} 2.5' in text
    assert 'celery_manager_rabbitmq_queue_deliver_rate{queue="work"} 1.5' in text
    # No rate sampled yet: left out, not reported as 0.
    assert 'celery_manager_rabbitmq_queue_publish_rate{queue="celery"}' not in text
    assert 'celery_manager_rabbitmq_disk_alarm{node="rabbit@a"} 1' in text
    assert 'celery_manager_rabbitmq_mem_alarm{node="rabbit@a"} 0' in text
    assert 'celery_manager_collector_up{collector="rabbitmq"} 1' in text


def test_metrics_skip_rabbitmq_when_not_configured(monkeypatch):
    from celery_manager import metrics as metrics_module

    monkeypatch.setattr(metrics_module, "list_queues", lambda *a, **k: {"broker": {"error": None}, "queues": []})
    monkeypatch.setattr(metrics_module, "_workers_family",
                        lambda *a, **k: metrics_module.Family("celery_manager_workers_online", "w", [({}, 1)]))
    config = _config()
    _, session_factory = create_app(config)
    assert "rabbitmq" not in render(compute_snapshot(config, session_factory))


def test_metrics_report_a_failing_management_api_as_down(mgmt, monkeypatch):
    from celery_manager import metrics as metrics_module

    mgmt.routes[("GET", "/api/queues//")] = (401, {})
    monkeypatch.setattr(metrics_module, "list_queues", lambda *a, **k: {"broker": {"error": None}, "queues": []})
    monkeypatch.setattr(metrics_module, "_workers_family",
                        lambda *a, **k: metrics_module.Family("celery_manager_workers_online", "w", [({}, 1)]))
    config = _config(mgmt)
    _, session_factory = create_app(config)
    text = render(compute_snapshot(config, session_factory))
    assert 'celery_manager_collector_up{collector="rabbitmq"} 0' in text
    assert PASSWORD not in text
