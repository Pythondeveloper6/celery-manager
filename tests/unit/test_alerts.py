"""Alerts: rule kinds fire and clear on their thresholds, the quiet period and
cross-process deduplication hold, channel failures stay isolated, webhook
signatures verify, secrets stay out of API responses, roles are enforced."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest
from alembic import command
from celery import Celery
from celery.schedules import crontab
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect

from celery_manager import cli
from celery_manager.alerts import DeliveryError, evaluate_once
from celery_manager.alerts import senders as senders_module
from celery_manager.alerts.senders import (
    AlertMessage,
    send_email,
    send_webhook,
    signature,
)
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import alerts as control
from celery_manager.db.migrate import alembic_config, upgrade_database
from celery_manager.db.models import (
    AlertChannel,
    AlertIncident,
    AlertRule,
    AuditLog,
    TaskRun,
)
from celery_manager.db.session import create_db_engine, get_session_factory, init_db

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
PW = "correct-horse-battery"
SLACK_URL = "https://hooks.slack.com/services/T000/B000/very-secret-token"


class FakeSenders:
    """Stands in for every channel kind; records what was sent and fails
    for the channel URLs/addresses listed in `failing`."""

    def __init__(self):
        self.sent: list[tuple[dict, AlertMessage]] = []
        self.failing: set[str] = set()
        self.lock = threading.Lock()

    def _send(self, channel_config, message, config):
        target = channel_config.get("url") or ",".join(channel_config.get("to", []))
        if target in self.failing:
            raise DeliveryError(f"{target} is down")
        with self.lock:
            self.sent.append((channel_config, message))

    def as_dict(self):
        return {"slack": self._send, "webhook": self._send, "email": self._send}

    def events(self):
        return [(m.event, m.key) for _, m in self.sent]


def _config(db_url, **kwargs):
    app = Celery("t")
    app.conf.beat_schedule = {"hourly": {"task": "demo.echo", "schedule": crontab(minute=0)}}
    return CeleryManagerConfig(celery_app=app, db_url=db_url, secret_key="s", **kwargs)


@pytest.fixture
def env(tmp_path):
    config = _config(f"sqlite:///{tmp_path / 'cm.db'}", dashboard_url="https://ops.example.com/cm/")
    engine = init_db(config)
    session_factory = get_session_factory(engine)
    fake = FakeSenders()
    yield config, session_factory, fake
    engine.dispose()


def _channel(session_factory, name="ops", kind="webhook", config=None):
    with session_factory() as session:
        return control.create_channel(session, "admin", name, kind, config or {"url": f"https://example.com/{name}"})


def _rule(session_factory, kind, channel_ids, **fields):
    payload = {"name": fields.pop("name", f"{kind} rule"), "kind": kind, "channel_ids": channel_ids, **fields}
    with session_factory() as session:
        return control.create_rule(session, "admin", payload)


def _runs(session_factory, task_name, succeeded, failed, at=NOW - timedelta(minutes=1)):
    with session_factory() as session:
        for i in range(succeeded + failed):
            status = "failed" if i < failed else "succeeded"
            session.add(TaskRun(task_id=f"{task_name}-{at.isoformat()}-{i}", task_name=task_name, status=status,
                                started_at=at, finished_at=at))
        session.commit()


def _evaluate(env, now=NOW, **kwargs):
    config, session_factory, fake = env
    return evaluate_once(session_factory, config.celery_app, config, now, senders=fake.as_dict(), **kwargs)


def _incidents(session_factory):
    with session_factory() as session:
        return session.query(AlertIncident).order_by(AlertIncident.id).all()


# --- failure_rate --------------------------------------------------------------------


def test_failure_rate_fires_once_over_threshold(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "failure_rate", [channel["id"]], params={"rate": 0.5, "min_runs": 4, "window_minutes": 15})
    _runs(sf, "reports.build", succeeded=1, failed=3)
    _runs(sf, "emails.send", succeeded=4, failed=1)

    _evaluate(env)
    assert fake.events() == [("firing", "reports.build")]
    message = fake.sent[0][1]
    assert "3 of 4 runs failed" in message.summary
    assert message.url == "https://ops.example.com/cm/#/task-types/reports.build"
    assert message.details["failure_rate"] == 0.75

    _evaluate(env, NOW + timedelta(minutes=1))  # still failing: no reminder
    assert len(fake.sent) == 1


def test_failure_rate_needs_min_runs_inside_the_window(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "failure_rate", [channel["id"]], params={"rate": 0.5, "min_runs": 5, "window_minutes": 15})
    _runs(sf, "reports.build", succeeded=0, failed=4)  # 100% but only 4 runs
    _runs(sf, "reports.build", succeeded=0, failed=10, at=NOW - timedelta(minutes=30))  # outside the window
    _evaluate(env)
    assert fake.sent == []
    assert _incidents(sf) == []


def test_failure_rate_target_and_glob(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "failure_rate", [channel["id"]], target="reports.*", params={"rate": 0.5, "min_runs": 1})
    _runs(sf, "reports.build", succeeded=0, failed=2)
    _runs(sf, "emails.send", succeeded=0, failed=2)
    _evaluate(env)
    assert fake.events() == [("firing", "reports.build")]


def test_resolve_sends_one_resolved_message(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "failure_rate", [channel["id"]], params={"rate": 0.5, "min_runs": 2, "window_minutes": 15})
    _runs(sf, "reports.build", succeeded=0, failed=2)
    _evaluate(env)
    later = NOW + timedelta(minutes=20)  # the failures left the window
    _evaluate(env, later)
    _evaluate(env, later + timedelta(minutes=1))
    assert fake.events() == [("firing", "reports.build"), ("resolved", "reports.build")]
    [incident] = _incidents(sf)
    assert incident.state == "resolved" and incident.open_key is None


def test_notify_resolved_off_sends_nothing_on_resolve(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "failure_rate", [channel["id"]], notify_resolved=False, params={"rate": 0.5, "min_runs": 2})
    _runs(sf, "reports.build", succeeded=0, failed=2)
    _evaluate(env)
    _evaluate(env, NOW + timedelta(minutes=20))
    assert fake.events() == [("firing", "reports.build")]
    assert _incidents(sf)[0].state == "resolved"


def test_quiet_period_holds_a_new_incident_then_sends_it_if_still_firing(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "failure_rate", [channel["id"]], quiet_period_minutes=60,
          params={"rate": 0.5, "min_runs": 2, "window_minutes": 15})
    _runs(sf, "reports.build", succeeded=0, failed=2)
    _evaluate(env)                                        # incident 1, announced
    _evaluate(env, NOW + timedelta(minutes=20))           # resolved
    _runs(sf, "reports.build", succeeded=0, failed=2, at=NOW + timedelta(minutes=29))
    _evaluate(env, NOW + timedelta(minutes=30))           # incident 2, inside the quiet period
    assert fake.events() == [("firing", "reports.build"), ("resolved", "reports.build")]
    second = _incidents(sf)[1]
    assert second.state == "firing" and second.notified_at is None

    # Still firing once the quiet period is over: announced then.
    _runs(sf, "reports.build", succeeded=0, failed=2, at=NOW + timedelta(minutes=60))
    _evaluate(env, NOW + timedelta(minutes=61))
    assert fake.events()[-1] == ("firing", "reports.build")


def test_incident_that_clears_inside_the_quiet_period_is_never_announced(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "failure_rate", [channel["id"]], quiet_period_minutes=60, params={"rate": 0.5, "min_runs": 2})
    _runs(sf, "reports.build", succeeded=0, failed=2)
    _evaluate(env)
    _evaluate(env, NOW + timedelta(minutes=20))
    _runs(sf, "reports.build", succeeded=0, failed=2, at=NOW + timedelta(minutes=29))
    _evaluate(env, NOW + timedelta(minutes=30))
    _evaluate(env, NOW + timedelta(minutes=50))  # cleared, still inside the quiet period
    assert fake.events() == [("firing", "reports.build"), ("resolved", "reports.build")]
    assert [i.state for i in _incidents(sf)] == ["resolved", "resolved"]


# --- queue_backlog -----------------------------------------------------------------------


def _queues(depths, error=None):
    def fake(celery_app, session, include_workers=True):
        return {
            "broker": {"error": error},
            "queues": [{"name": n, "depth": d} for n, d in depths.items()],
        }
    return fake


def test_queue_backlog_fires_on_depth_and_resolves(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "queue_backlog", [channel["id"]], params={"depth": 100})
    _evaluate(env, list_queues_fn=_queues({"celery": 150, "emails": 99}))
    assert fake.events() == [("firing", "celery")]
    assert "150 waiting messages" in fake.sent[0][1].summary
    _evaluate(env, NOW + timedelta(minutes=1), list_queues_fn=_queues({"celery": 3, "emails": 99}))
    assert fake.events()[-1] == ("resolved", "celery")


def test_queue_backlog_unknown_depth_neither_fires_nor_resolves(env):
    _, sf, fake = env
    channel = _channel(sf)
    rule = _rule(sf, "queue_backlog", [channel["id"]], target="celery", params={"depth": 100})
    _evaluate(env, list_queues_fn=_queues({"celery": 150}))
    _evaluate(env, NOW + timedelta(minutes=1), list_queues_fn=_queues({"celery": None}, error="timed out"))
    assert fake.events() == [("firing", "celery")]
    assert _incidents(sf)[0].state == "firing"
    with sf() as session:
        assert "timed out" in session.get(AlertRule, rule["id"]).last_check_error


# --- worker_offline ----------------------------------------------------------------------


def _workers(*names):
    def fake(celery_app, timeout):
        return [{"name": n, "online": True} for n in names]
    return fake


def test_worker_offline_after_threshold_and_back(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "worker_offline", [channel["id"]], target="celery@web-*", params={"minutes": 5})
    _evaluate(env, inspect_workers_fn=_workers("celery@web-1", "celery@web-2", "celery@batch"))
    _evaluate(env, NOW + timedelta(minutes=3), inspect_workers_fn=_workers("celery@web-1"))
    assert fake.sent == []  # gone 3 minutes: under the threshold
    _evaluate(env, NOW + timedelta(minutes=6), inspect_workers_fn=_workers("celery@web-1"))
    assert fake.events() == [("firing", "celery@web-2")]  # batch doesn't match the target
    assert fake.sent[0][1].url == "https://ops.example.com/cm/#/workers"
    _evaluate(env, NOW + timedelta(minutes=7), inspect_workers_fn=_workers("celery@web-1", "celery@web-2"))
    assert fake.events()[-1] == ("resolved", "celery@web-2")


def test_worker_offline_ping_failure_is_reported_not_fired(env):
    _, sf, fake = env
    channel = _channel(sf)
    rule = _rule(sf, "worker_offline", [channel["id"]])

    def broken(celery_app, timeout):
        raise RuntimeError("broker unreachable")

    _evaluate(env, inspect_workers_fn=broken)
    assert fake.sent == []
    with sf() as session:
        assert "broker unreachable" in session.get(AlertRule, rule["id"]).last_check_error


# --- missed_run --------------------------------------------------------------------------


def _stamped(sf, hour):
    at = datetime(2026, 10, 28, hour, 0, tzinfo=timezone.utc)
    with sf() as session:
        session.add(TaskRun(task_id=f"hourly-{hour}", task_name="demo.echo", status="succeeded",
                            schedule_name="hourly", started_at=at, occurrence_at=at, beat_sent_at=at))
        session.commit()


def test_missed_run_fires_and_clears_with_the_window(env):
    _, sf, fake = env
    channel = _channel(sf)
    _rule(sf, "missed_run", [channel["id"]], params={"window_minutes": 120})
    for hour in (8, 9, 11):  # 10:00 never arrived
        _stamped(sf, hour)
    now = datetime(2026, 10, 28, 11, 30, tzinfo=timezone.utc)
    _evaluate(env, now)
    assert fake.events() == [("firing", "hourly")]
    assert fake.sent[0][1].url == "https://ops.example.com/cm/#/schedules/hourly"
    assert fake.sent[0][1].details["missed"] == 1
    for hour in (12, 13):
        _stamped(sf, hour)
    _evaluate(env, datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc))
    assert fake.events()[-1] == ("resolved", "hourly")


def test_missed_run_quiet_when_every_run_arrived(env):
    _, sf, fake = env
    _rule(sf, "missed_run", [_channel(sf)["id"]])
    for hour in (9, 10, 11):
        _stamped(sf, hour)
    _evaluate(env, datetime(2026, 10, 28, 11, 30, tzinfo=timezone.utc))
    assert fake.sent == []


# --- deduplication, failures -------------------------------------------------------------


def test_two_evaluators_racing_send_one_message(tmp_path):
    url = f"sqlite:///{tmp_path / 'race.db'}"
    config = _config(url)
    init_db(config).dispose()
    engines = [create_db_engine(url) for _ in range(4)]
    factories = [get_session_factory(e) for e in engines]
    channel = _channel(factories[0])
    _rule(factories[0], "failure_rate", [channel["id"]], params={"rate": 0.5, "min_runs": 2})
    _runs(factories[0], "reports.build", succeeded=0, failed=2)
    fake = FakeSenders()
    barrier = threading.Barrier(len(factories))
    errors = []

    def run(sf):
        try:
            barrier.wait()
            evaluate_once(sf, config.celery_app, config, NOW, senders=fake.as_dict())
        except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(sf,)) for sf in factories]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert fake.events() == [("firing", "reports.build")]
    assert len(_incidents(factories[0])) == 1
    for engine in engines:
        engine.dispose()


def test_second_evaluator_after_the_first_sends_nothing(tmp_path):
    url = f"sqlite:///{tmp_path / 'two.db'}"
    config = _config(url)
    first, second = init_db(config), create_db_engine(url)
    sf1, sf2 = get_session_factory(first), get_session_factory(second)
    channel = _channel(sf1)
    _rule(sf1, "failure_rate", [channel["id"]], params={"rate": 0.5, "min_runs": 2})
    _runs(sf1, "reports.build", succeeded=0, failed=2)
    fake = FakeSenders()
    evaluate_once(sf1, config.celery_app, config, NOW, senders=fake.as_dict())
    evaluate_once(sf2, config.celery_app, config, NOW, senders=fake.as_dict())
    evaluate_once(sf2, config.celery_app, config, NOW + timedelta(minutes=20), senders=fake.as_dict())
    evaluate_once(sf1, config.celery_app, config, NOW + timedelta(minutes=20), senders=fake.as_dict())
    assert fake.events() == [("firing", "reports.build"), ("resolved", "reports.build")]
    first.dispose()
    second.dispose()


def test_failing_channel_does_not_block_the_others(env):
    _, sf, fake = env
    broken = _channel(sf, "broken", config={"url": "https://down.example.com/hook"})
    working = _channel(sf, "working", config={"url": "https://up.example.com/hook"})
    _rule(sf, "failure_rate", [broken["id"], working["id"]], params={"rate": 0.5, "min_runs": 2})
    _runs(sf, "reports.build", succeeded=0, failed=2)
    fake.failing.add("https://down.example.com/hook")
    _evaluate(env)
    assert [c["url"] for c, _ in fake.sent] == ["https://up.example.com/hook"]
    with sf() as session:
        assert "down.example.com/hook is down" in session.get(AlertChannel, broken["id"]).last_error
        assert session.get(AlertChannel, working["id"]).last_sent_at is not None
        assert session.get(AlertChannel, working["id"]).last_error is None
    [incident] = _incidents(sf)
    assert "broken" in incident.delivery_error and incident.notified_at is not None
    _evaluate(env, NOW + timedelta(minutes=1))  # partial failure is not retried
    assert len(fake.sent) == 1


def test_all_channels_failing_retries_on_the_next_pass(env):
    _, sf, fake = env
    channel = _channel(sf, config={"url": "https://down.example.com/hook"})
    _rule(sf, "failure_rate", [channel["id"]], params={"rate": 0.5, "min_runs": 2})
    _runs(sf, "reports.build", succeeded=0, failed=2)
    fake.failing.add("https://down.example.com/hook")
    _evaluate(env)
    assert fake.sent == [] and _incidents(sf)[0].notified_at is None
    fake.failing.clear()
    _evaluate(env, NOW + timedelta(minutes=1))
    assert fake.events() == [("firing", "reports.build")]


def test_a_broken_rule_does_not_stop_the_others(env, monkeypatch):
    _, sf, fake = env
    channel = _channel(sf)
    broken = _rule(sf, "queue_backlog", [channel["id"]], name="a broken")
    _rule(sf, "failure_rate", [channel["id"]], name="b fine", params={"rate": 0.5, "min_runs": 2})
    _runs(sf, "reports.build", succeeded=0, failed=2)

    def explode(celery_app, session, include_workers=True):
        raise ZeroDivisionError("boom")

    _evaluate(env, list_queues_fn=explode)
    assert fake.events() == [("firing", "reports.build")]
    with sf() as session:
        assert "boom" in session.get(AlertRule, broken["id"]).last_check_error


def test_disabling_a_rule_closes_its_incidents_silently(env):
    _, sf, fake = env
    channel = _channel(sf)
    rule = _rule(sf, "failure_rate", [channel["id"]], params={"rate": 0.5, "min_runs": 2})
    _runs(sf, "reports.build", succeeded=0, failed=2)
    _evaluate(env)
    with sf() as session:
        control.update_rule(session, "admin", rule["id"], {"enabled": False})
    _evaluate(env, NOW + timedelta(minutes=1))
    assert fake.events() == [("firing", "reports.build")]
    assert _incidents(sf)[0].state == "resolved"


def test_deleting_a_channel_removes_it_from_rules(env):
    _, sf, _ = env
    a, b = _channel(sf, "a"), _channel(sf, "b")
    rule = _rule(sf, "failure_rate", [a["id"], b["id"]])
    with sf() as session:
        control.delete_channel(session, "admin", a["id"])
        assert json.loads(session.get(AlertRule, rule["id"]).channel_ids) == [b["id"]]


# --- senders ---------------------------------------------------------------------------


class _Capture(BaseHTTPRequestHandler):
    requests: ClassVar[list] = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).requests.append((dict(self.headers), body))
        self.send_response(type(self).status)
        if type(self).status in (301, 302):
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
        self.end_headers()
        self.wfile.write(b"nope" if type(self).status >= 300 else b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    _Capture.requests = []
    _Capture.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook", _Capture
    server.shutdown()
    server.server_close()


def test_webhook_is_signed_with_hmac_sha256(http_server):
    url, capture = http_server
    config = _config("sqlite://")
    message = AlertMessage(event="firing", summary="hello", key="reports.build", details={"failed": 3})
    send_webhook({"url": url, "secret": "s3cret"}, message, config)
    [(headers, body)] = capture.requests
    timestamp = headers["X-Celery-Manager-Timestamp"]
    expected = "sha256=" + hmac.new(b"s3cret", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["X-Celery-Manager-Signature"] == expected == signature("s3cret", timestamp, body)
    payload = json.loads(body)
    assert payload["schema"] == 1 and payload["event"] == "alert.firing" and payload["key"] == "reports.build"


def test_webhook_without_secret_is_unsigned(http_server):
    url, capture = http_server
    send_webhook({"url": url}, AlertMessage(event="test", summary="hi"), _config("sqlite://"))
    [(headers, _)] = capture.requests
    assert "X-Celery-Manager-Signature" not in headers


def test_webhook_error_status_and_redirects_are_failures(http_server):
    url, capture = http_server
    capture.status = 500
    with pytest.raises(DeliveryError, match="HTTP 500: nope"):
        send_webhook({"url": url}, AlertMessage(event="test", summary="hi"), _config("sqlite://"))
    capture.status = 302
    with pytest.raises(DeliveryError, match="302"):
        send_webhook({"url": url}, AlertMessage(event="test", summary="hi"), _config("sqlite://"))
    assert len(capture.requests) == 2  # the redirect was not followed


def test_slack_payload_has_text_and_link(http_server):
    url, capture = http_server
    message = AlertMessage(event="firing", summary="Queue <x> & y", url="https://ops.example.com/#/queues")
    senders_module.send_slack({"url": url}, message, _config("sqlite://"))
    body = json.loads(capture.requests[0][1])
    assert body["text"] == "[ALERT] Queue <x> & y"
    block = body["blocks"][0]["text"]["text"]
    assert "Queue &lt;x&gt; &amp; y" in block and "<https://ops.example.com/#/queues|Open in the dashboard>" in block


def test_email_uses_the_configured_smtp_server(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, email):
            calls.append(("send", email["To"], email["From"], email["Subject"]))

    monkeypatch.setattr(senders_module.smtplib, "SMTP", FakeSMTP)
    config = _config("sqlite://", smtp_host="mail.example.com", smtp_username="bot", smtp_password="pw",
                     alert_email_from="alerts@example.com", alert_send_timeout_seconds=3)
    send_email({"to": ["a@example.com", "b@example.com"]}, AlertMessage(event="firing", summary="x"), config)
    assert calls == [
        ("connect", "mail.example.com", 587, 3),
        ("starttls",),
        ("login", "bot", "pw"),
        ("send", "a@example.com, b@example.com", "alerts@example.com", "[ALERT] x"),
    ]
    assert "pw" not in repr(config)


def test_email_without_smtp_host_says_so():
    with pytest.raises(DeliveryError, match="smtp_host"):
        send_email({"to": ["a@example.com"]}, AlertMessage(event="test", summary="x"), _config("sqlite://"))


# --- validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "config", "fragment"),
    [
        ("slack", {"url": "file:///etc/passwd"}, "https://"),
        ("webhook", {"url": "gopher://x"}, "https://"),
        ("webhook", {}, "required"),
        ("email", {"to": ["not-an-address"]}, "not an email"),
        ("email", {"to": ["a@example.com\r\nBcc: x@evil.com"]}, "not an email"),
        ("sms", {}, "kind"),
    ],
)
def test_channel_validation(env, kind, config, fragment):
    _, sf, _ = env
    with sf() as session, pytest.raises(ValueError, match=fragment):
        control.create_channel(session, "admin", "x", kind, config)


@pytest.mark.parametrize(
    ("params", "fragment"),
    [({"rate": 2}, "between"), ({"treshold": 1}, "unknown"), ({"min_runs": 1.5}, "whole"), ({"rate": "x"}, "number")],
)
def test_rule_param_validation(env, params, fragment):
    _, sf, _ = env
    with sf() as session, pytest.raises(ValueError, match=fragment):
        control.create_rule(session, "admin", {"name": "r", "kind": "failure_rate", "params": params})


def test_rule_defaults_are_filled_in(env):
    _, sf, _ = env
    rule = _rule(sf, "failure_rate", [])
    assert rule["params"] == {"rate": 0.2, "min_runs": 10, "window_minutes": 15}
    assert rule["target"] == "*" and rule["quiet_period_minutes"] == 60 and rule["state"] == "ok"


# --- API --------------------------------------------------------------------------------


@pytest.fixture
def api():
    config = _config("sqlite:///:memory:")
    app, session_factory = create_app(config)
    with session_factory() as session:
        for name, role in (("root", "admin"), ("op", "operator"), ("view", "viewer")):
            create_user(session, name, PW, role=role)
    return app, session_factory


def _client(app, username):
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    assert client.post("/api/auth/login", json={"username": username, "password": PW}).status_code == 200
    return client


def test_api_channel_secrets_are_write_only(api):
    app, sf = api
    admin = _client(app, "root")
    created = admin.post("/api/alerts/channels", json={
        "name": "hooks", "kind": "webhook", "config": {"url": SLACK_URL, "secret": "s3cret"}})
    assert created.status_code == 201
    slack = admin.post("/api/alerts/channels", json={"name": "slack", "kind": "slack", "config": {"url": SLACK_URL}})
    for response in (created, slack, admin.get("/api/alerts/channels"), _client(app, "view").get("/api/alerts/channels")):
        text = response.text
        assert "very-secret-token" not in text and "s3cret" not in text
    entry = created.json()
    assert entry["config"] == {"url": "https://hooks.slack.com/…", "secret_set": True}

    # Renaming keeps the stored URL and secret it never saw.
    renamed = admin.patch(f"/api/alerts/channels/{entry['id']}", json={"name": "renamed", "config": {}})
    assert renamed.status_code == 200 and renamed.json()["config"]["secret_set"] is True
    with sf() as session:
        stored = json.loads(session.get(AlertChannel, entry["id"]).config)
    assert stored == {"url": SLACK_URL, "secret": "s3cret"}
    cleared = admin.patch(f"/api/alerts/channels/{entry['id']}", json={"config": {"secret": ""}})
    assert cleared.json()["config"]["secret_set"] is False

    with sf() as session:
        details = [r.details for r in session.query(AuditLog).filter(AuditLog.target_type == "alert_channel")]
    assert details and all("very-secret-token" not in d and "s3cret" not in d for d in details)


@pytest.mark.parametrize("user", ["view", "op"])
def test_api_writes_need_admin(api, user):
    app, _ = api
    client = _client(app, user)
    assert client.get("/api/alerts/rules").status_code == 200
    assert client.get("/api/alerts/channels").status_code == 200
    assert client.get("/api/alerts/incidents").status_code == 200
    response = client.post("/api/alerts/rules", json={"name": "r", "kind": "failure_rate"})
    assert response.status_code == 403 and "admin" in response.json()["detail"]
    assert client.post("/api/alerts/channels", json={"name": "c", "kind": "slack", "config": {"url": SLACK_URL}}).status_code == 403
    assert client.post("/api/alerts/channels/1/test").status_code == 403
    assert client.delete("/api/alerts/rules/1").status_code == 403


def test_api_rule_crud_is_audited(api):
    app, sf = api
    admin = _client(app, "root")
    channel = admin.post("/api/alerts/channels", json={"name": "c", "kind": "slack", "config": {"url": SLACK_URL}}).json()
    created = admin.post("/api/alerts/rules", json={
        "name": "slow queue", "kind": "queue_backlog", "target": "celery",
        "params": {"depth": 500}, "channel_ids": [channel["id"]]})
    assert created.status_code == 201
    rule = created.json()
    assert rule["params"] == {"depth": 500} and rule["channel_ids"] == [channel["id"]]
    assert admin.post("/api/alerts/rules", json={"name": "slow queue", "kind": "queue_backlog"}).status_code == 409
    bad = admin.post("/api/alerts/rules", json={"name": "x", "kind": "queue_backlog", "channel_ids": [999]})
    assert bad.status_code == 422 and "999" in bad.json()["detail"]
    updated = admin.patch(f"/api/alerts/rules/{rule['id']}", json={"params": {"depth": 10}, "enabled": False})
    assert updated.json()["params"] == {"depth": 10} and updated.json()["state"] == "disabled"
    assert admin.delete(f"/api/alerts/rules/{rule['id']}").status_code == 200
    assert admin.get("/api/alerts/rules").json()["rules"] == []
    assert admin.delete(f"/api/alerts/rules/{rule['id']}").status_code == 404
    with sf() as session:
        actions = [r.action for r in session.query(AuditLog).order_by(AuditLog.id)]
    assert actions == ["create_alert_channel", "create_alert_rule", "update_alert_rule", "delete_alert_rule"]


def test_api_test_message(api, monkeypatch):
    app, sf = api
    admin = _client(app, "root")
    fake = FakeSenders()
    monkeypatch.setitem(senders_module.SENDERS, "slack", fake._send)
    channel = admin.post("/api/alerts/channels", json={"name": "c", "kind": "slack", "config": {"url": SLACK_URL}}).json()
    assert admin.post(f"/api/alerts/channels/{channel['id']}/test").json() == {"ok": True}
    assert fake.sent[0][1].event == "test"
    fake.failing.add(SLACK_URL)
    failed = admin.post(f"/api/alerts/channels/{channel['id']}/test")
    assert failed.status_code == 502 and "is down" in failed.json()["detail"]
    listed = admin.get("/api/alerts/channels").json()["channels"][0]
    assert "is down" in listed["last_error"]
    assert admin.post("/api/alerts/channels/999/test").status_code == 404
    with sf() as session:
        assert session.query(AuditLog).filter_by(action="test_alert_channel").count() == 2


def test_api_incidents_and_rule_state(api):
    app, sf = api
    config = app.state.cm_config
    fake = FakeSenders()
    with sf() as session:
        control.create_rule(session, "root", {"name": "fails", "kind": "failure_rate",
                                               "params": {"rate": 0.5, "min_runs": 1}})
    _runs(sf, "reports.build", succeeded=0, failed=1, at=datetime.now(timezone.utc) - timedelta(minutes=1))
    evaluate_once(sf, config.celery_app, config, senders=fake.as_dict())
    viewer = _client(app, "view")
    [rule] = viewer.get("/api/alerts/rules").json()["rules"]
    assert rule["state"] == "firing" and rule["firing_keys"] == ["reports.build"]
    [incident] = viewer.get("/api/alerts/incidents?state=firing").json()["incidents"]
    assert incident["rule_name"] == "fails" and incident["key"] == "reports.build"
    assert viewer.get("/api/alerts/incidents?state=resolved").json()["incidents"] == []
    assert viewer.get("/api/alerts/incidents?state=bogus").status_code == 422


# --- migration, CLI ----------------------------------------------------------------------


def test_migration_up_and_down(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_database(engine)
    tables = {"alert_channels", "alert_rules", "alert_incidents", "alert_worker_sightings"}
    assert tables <= set(sa_inspect(engine).get_table_names())
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "0007")
    assert not tables & set(sa_inspect(engine).get_table_names())
    upgrade_database(engine)
    assert tables <= set(sa_inspect(engine).get_table_names())
    engine.dispose()


def test_cli_alerts_once(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setattr(sys, "argv", ["celery-manager", "alerts", "--app", "celery:Celery", "--db-url", url, "--once"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "0 message(s) sent." in capsys.readouterr().out


def test_cli_needs_an_app_or_a_config(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["celery-manager", "alerts", "--once"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert "--config" in str(exc.value.code)
