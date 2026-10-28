"""Broker health: Redis/AMQP/other branches, never raising, no credential leaks,
the API, the metrics, the two alert kinds, and the filesystem depth fix.

Unit tests use fakes at the one seam the control layer uses
(`celery_app.connection_for_read`). The integration tests at the bottom talk to
real brokers and are skipped unless CM_TEST_REDIS_URL / CM_TEST_AMQP_URL is set.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from celery import Celery
from fastapi.testclient import TestClient

import celery_manager.metrics as metrics_module
from celery_manager.alerts import evaluate_once
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import alerts as alerts_control
from celery_manager.control import broker as broker_module
from celery_manager.control.broker import broker_health, redis_details
from celery_manager.db.models import AlertIncident, AlertRule
from celery_manager.db.session import get_session_factory, init_db
from celery_manager.metrics import compute_snapshot, render

PASSWORD = "hunter2-very-secret"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


# --- fakes --------------------------------------------------------------------


class SentinelTransport:
    """Named like kombu's class: that is how a Sentinel connection is told apart."""

    driver_type = "redis"


class RedisTransport:
    driver_type = "redis"


class AmqpTransport:
    driver_type = "amqp"


class OtherTransport:
    driver_type = "sqs"


def redis_info(**overrides):
    info = {
        "redis_version": "7.2.4",
        "used_memory": 50_000_000,
        "maxmemory": 100_000_000,
        "maxmemory_policy": "noeviction",
        "connected_clients": 12,
        "blocked_clients": 3,
        "evicted_keys": 0,
        "rejected_connections": 0,
        "uptime_in_seconds": 86_400,
        "rdb_last_bgsave_status": "ok",
        "rdb_last_save_time": 1_790_000_000,
        "rdb_changes_since_last_save": 5,
        "aof_enabled": 0,
    }
    info.update(overrides)
    return info


def fake_app(transport=None, info=None, connect_error=None, props=None, ping_error=None, password=PASSWORD):
    transport = transport or RedisTransport()
    conn = MagicMock()
    conn.transport = transport
    conn.password = password
    conn.userid = None
    conn.as_uri.return_value = "redis://:**@broker.example:6379/0"
    if connect_error is not None:
        conn.connect.side_effect = connect_error
    client = MagicMock()
    client.info.return_value = info if info is not None else redis_info()
    if ping_error is not None:
        client.ping.side_effect = ping_error
    conn.default_channel.client = client
    conn.connection.server_properties = props or {}
    app = MagicMock()
    app.connection_for_read.return_value = conn
    return app, conn, client


# --- Redis ----------------------------------------------------------------------


def test_redis_healthy_noeviction_has_no_warnings():
    app, conn, _ = fake_app()
    health = broker_health(app)
    assert health["reachable"] is True and health["transport"] == "redis"
    assert health["sentinel"] is False and health["error"] is None
    assert health["latency_ms"] >= 0
    d = health["details"]
    assert d["version"] == "7.2.4"
    assert d["used_memory"] == 50_000_000 and d["maxmemory"] == 100_000_000
    assert d["memory_ratio"] == 0.5
    assert d["maxmemory_policy"] == "noeviction"
    assert (d["connected_clients"], d["blocked_clients"]) == (12, 3)
    assert (d["evicted_keys"], d["rejected_connections"], d["uptime_seconds"]) == (0, 0, 86_400)
    assert d["persistence"]["rdb_last_save_status"] == "ok"
    assert d["persistence"]["rdb_last_save_at"].startswith("2026-")
    assert d["persistence"]["aof_enabled"] is False
    assert d["warnings"] == []
    conn.release.assert_called_once()


def test_redis_eviction_policy_other_than_noeviction_warns_in_plain_language():
    app, _, _ = fake_app(info=redis_info(maxmemory_policy="allkeys-lru"))
    d = broker_health(app)["details"]
    assert len(d["warnings"]) == 1
    warning = d["warnings"][0]
    assert "allkeys-lru" in warning and "queued tasks" in warning and "noeviction" in warning
    assert "No memory limit" not in warning


def test_redis_policy_warning_with_no_limit_says_nothing_is_evicted_today():
    app, _, _ = fake_app(info=redis_info(maxmemory=0, maxmemory_policy="volatile-lru"))
    d = broker_health(app)["details"]
    assert d["maxmemory"] == 0
    assert d["memory_ratio"] is None  # no limit, no ratio to report
    assert "No memory limit is set" in d["warnings"][0]


def test_redis_maxmemory_zero_noeviction_is_quiet():
    app, _, _ = fake_app(info=redis_info(maxmemory=0))
    d = broker_health(app)["details"]
    assert d["memory_ratio"] is None and d["warnings"] == []


def test_redis_evicted_keys_and_refused_connections_and_failed_snapshot_warn():
    d = redis_details(redis_info(evicted_keys=42, rejected_connections=7, rdb_last_bgsave_status="err"))
    text = " ".join(d["warnings"])
    assert "42" in text and "evicted" in text
    assert "refused 7 connections" in text
    assert "RDB snapshot failed" in text


def test_redis_aof_status_is_reported():
    d = redis_details(redis_info(aof_enabled=1, aof_last_write_status="err"))
    assert d["persistence"]["aof_enabled"] is True
    assert d["persistence"]["aof_last_write_status"] == "err"
    assert any("append-only" in w for w in d["warnings"])


def test_redis_missing_info_fields_are_none_not_errors():
    d = redis_details({"redis_version": "6.0.0"})
    assert d["used_memory"] is None and d["memory_ratio"] is None and d["evicted_keys"] is None
    assert d["maxmemory"] == 0


def test_sentinel_is_tagged_and_reads_the_master_through_the_same_client():
    app, _, client = fake_app(transport=SentinelTransport())
    health = broker_health(app)
    assert health["transport"] == "redis" and health["sentinel"] is True
    assert health["details"]["version"] == "7.2.4"
    client.info.assert_called_once()


# --- RabbitMQ and others -----------------------------------------------------------


def test_amqp_reports_server_properties_and_the_management_hint():
    app, _, _ = fake_app(
        transport=AmqpTransport(),
        props={"product": b"RabbitMQ", "version": "3.13.7", "cluster_name": "rabbit@host"},
    )
    health = broker_health(app)
    assert health["reachable"] and health["transport"] == "amqp"
    assert health["details"] == {
        "product": "RabbitMQ",
        "version": "3.13.7",
        "cluster_name": "rabbit@host",
        "management": "not configured",
        "warnings": [],
    }


def test_other_transport_is_reachable_with_a_note():
    app, _, _ = fake_app(transport=OtherTransport())
    health = broker_health(app)
    assert health["reachable"] is True
    assert health["details"] is None
    assert health["details_note"] == "details not available for sqs"


def test_memory_transport_says_details_not_available():
    health = broker_health(Celery("t", broker="memory://"))
    assert health["reachable"] and health["transport"] == "memory"
    assert health["details_note"] == "details not available for memory"


# --- failure never raises, never leaks ------------------------------------------------


def test_unreachable_broker_is_a_result_not_an_exception_and_hides_the_password():
    app, _, _ = fake_app(connect_error=OSError(f"cannot connect with {PASSWORD} at broker.example"))
    health = broker_health(app)
    assert health["reachable"] is False
    assert health["latency_ms"] is None and health["details"] is None
    assert health["error"].startswith("could not reach the broker")
    assert PASSWORD not in json.dumps(health)
    assert "***" in health["error"]


def test_redis_command_failure_after_connect_is_unreachable():
    app, _, _ = fake_app(ping_error=ConnectionError("connection reset"))
    health = broker_health(app)
    assert health["reachable"] is False and "connection reset" in health["error"]


def test_real_unreachable_redis_url_never_shows_the_password():
    app = Celery("t", broker=f"redis://:{PASSWORD}@127.0.0.1:1/0")
    health = broker_health(app)
    assert health["reachable"] is False
    assert health["transport"] == "redis"
    assert PASSWORD not in json.dumps(health)
    assert health["url"] and "127.0.0.1:1" in health["url"]


def test_malformed_url_still_answers():
    health = broker_health(Celery("t", broker="not-a-real-transport://x"))
    assert health["reachable"] is False and health["error"]


def test_a_broker_that_goes_silent_hits_the_deadline():
    release = threading.Event()
    app, conn, _ = fake_app()
    conn.connect.side_effect = lambda: release.wait(5)
    started = time.monotonic()
    health = broker_health(app, timeout=0.2)
    release.set()
    assert time.monotonic() - started < 2
    assert health["reachable"] is False
    assert "did not answer within 0.2s" in health["error"]


def test_anything_unexpected_inside_the_probe_is_contained(monkeypatch):
    def boom(_app):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(broker_module, "_probe", boom)
    health = broker_health(Celery("t", broker="memory://"))
    assert health["reachable"] is False and "kaboom" in health["error"]


# --- the filesystem depth fix ----------------------------------------------------------


def _filesystem_app(tmp_path):
    folder = tmp_path / "broker"
    folder.mkdir()
    app = Celery("t", broker="filesystem://")
    app.conf.broker_transport_options = {"data_folder_in": str(folder), "data_folder_out": str(folder)}
    return app


def test_filesystem_transport_says_depth_isnt_available_instead_of_zero(tmp_path):
    from celery_manager.control.queues import list_queues

    app = _filesystem_app(tmp_path)
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s")
    _, session_factory = create_app(config)
    with session_factory() as session:
        listing = list_queues(app, session, include_workers=False)
    assert listing["broker"]["unsupported"] is True
    assert listing["broker"]["transport"] == "filesystem"
    assert "queue depth isn't available" in listing["broker"]["error"]
    celery = next(q for q in listing["queues"] if q["name"] == "celery")
    assert celery["depth"] is None  # not 0: that would hide a backlog
    assert "queue depth isn't available" in celery["reason"]


def test_filesystem_purge_is_refused_with_the_same_reason(tmp_path):
    from celery_manager.control.queues import (
        QueueBrokerError,
        _unsupported_reason,
        purge_queue,
    )

    assert "queue depth isn't available" in _unsupported_reason("filesystem")
    assert _unsupported_reason("redis") is None and _unsupported_reason("amqp") is None
    app = _filesystem_app(tmp_path)
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s")
    _, session_factory = create_app(config)
    with session_factory() as session, pytest.raises(QueueBrokerError):
        purge_queue(app, session, "celery", "alice")


# --- API ---------------------------------------------------------------------------------


def _client(broker="memory://", role="viewer"):
    celery_app = Celery("t", broker=broker)
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="s")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "bob", "pw", role=role)
    client = TestClient(app, base_url="https://testserver")
    return client


def test_api_requires_sign_in():
    assert _client().get("/api/broker/health").status_code == 401


@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
def test_api_is_open_to_every_signed_in_role(role):
    client = _client(role=role)
    client.post("/api/auth/login", json={"username": "bob", "password": "pw"})
    response = client.get("/api/broker/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "transport", "reachable", "latency_ms", "url", "error", "sentinel", "details", "details_note", "checked_at",
    }
    assert body["reachable"] is True and body["transport"] == "memory"


def test_api_answers_200_for_a_dead_broker_and_leaks_no_password():
    client = _client(broker=f"redis://:{PASSWORD}@127.0.0.1:1/0")
    client.post("/api/auth/login", json={"username": "bob", "password": "pw"})
    response = client.get("/api/broker/health")
    assert response.status_code == 200
    assert response.json()["reachable"] is False
    assert PASSWORD not in response.text


# --- metrics --------------------------------------------------------------------------------


def _families(monkeypatch, health):
    monkeypatch.setattr(metrics_module, "broker_health", lambda celery_app: health)
    monkeypatch.setattr(
        metrics_module, "list_queues",
        lambda *a, **k: {"broker": {"error": None}, "queues": []},
    )
    monkeypatch.setattr(metrics_module, "_workers_family", lambda *a, **k: metrics_module.Family("celery_manager_workers_online", "x", [({}, 0)]))
    app = Celery("t", broker="memory://")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s")
    _, session_factory = create_app(config)
    return {f.name: f for f in compute_snapshot(config, session_factory)}


def _health(**overrides):
    base = {"transport": "redis", "reachable": True, "latency_ms": 12.5, "details": redis_details(redis_info())}
    base.update(overrides)
    return base


def test_metrics_for_redis(monkeypatch):
    families = _families(monkeypatch, _health())
    assert families["celery_manager_broker_up"].samples == [({"transport": "redis"}, 1)]
    assert families["celery_manager_broker_latency_seconds"].samples == [({"transport": "redis"}, 0.0125)]
    assert families["celery_manager_broker_memory_used_bytes"].samples == [({"transport": "redis"}, 50_000_000)]
    assert families["celery_manager_broker_memory_max_bytes"].samples == [({"transport": "redis"}, 100_000_000)]
    assert families["celery_manager_broker_evicted_keys"].samples == [({"transport": "redis"}, 0)]
    assert families["celery_manager_broker_connected_clients"].samples == [({"transport": "redis"}, 12)]
    text = render(list(families.values()))
    assert 'celery_manager_broker_up{transport="redis"} 1' in text
    up = {s[0]["collector"]: s[1] for s in families["celery_manager_collector_up"].samples}
    assert up["broker"] == 1


def test_metrics_unlimited_redis_reports_max_zero(monkeypatch):
    details = redis_details(redis_info(maxmemory=0))
    families = _families(monkeypatch, _health(details=details))
    assert families["celery_manager_broker_memory_max_bytes"].samples == [({"transport": "redis"}, 0)]


def test_metrics_down_broker_is_up_zero_and_nothing_else(monkeypatch):
    families = _families(monkeypatch, {"transport": "redis", "reachable": False, "latency_ms": None, "details": None})
    assert families["celery_manager_broker_up"].samples == [({"transport": "redis"}, 0)]
    for name in (
        "latency_seconds", "memory_used_bytes", "memory_max_bytes", "evicted_keys", "connected_clients",
    ):
        assert families[f"celery_manager_broker_{name}"].samples == []
    # The collector worked; the answer was "down", so collector_up stays 1.
    up = {s[0]["collector"]: s[1] for s in families["celery_manager_collector_up"].samples}
    assert up["broker"] == 1


def test_metrics_non_redis_has_up_and_latency_only(monkeypatch):
    families = _families(monkeypatch, _health(transport="amqp", details={"product": "RabbitMQ"}))
    assert families["celery_manager_broker_up"].samples == [({"transport": "amqp"}, 1)]
    assert families["celery_manager_broker_latency_seconds"].samples
    assert families["celery_manager_broker_memory_used_bytes"].samples == []


def test_metric_names_include_the_broker_family():
    for name in ("up", "latency_seconds", "memory_used_bytes", "memory_max_bytes", "evicted_keys", "connected_clients"):
        assert f"celery_manager_broker_{name}" in metrics_module.METRIC_NAMES


# --- alerts -----------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    app = Celery("t", broker="memory://")
    config = CeleryManagerConfig(celery_app=app, db_url=f"sqlite:///{tmp_path / 'cm.db'}", secret_key="s")
    engine = init_db(config)
    yield config, get_session_factory(engine)
    engine.dispose()


class Sent:
    def __init__(self):
        self.messages = []

    def __call__(self, channel_config, message, config):
        self.messages.append(message)

    def senders(self):
        return {"webhook": self, "slack": self, "email": self}

    def events(self):
        return [(m.event, m.key) for m in self.messages]


def _rule(sf, kind, **fields):
    with sf() as session:
        channel = alerts_control.create_channel(session, "admin", f"ch-{kind}", "webhook", {"url": "https://example.com/h"})
        return alerts_control.create_rule(
            session, "admin", {"name": kind, "kind": kind, "channel_ids": [channel["id"]], **fields}
        )


def _run(env, sent, health, now):
    config, sf = env
    return evaluate_once(sf, config.celery_app, config, now, senders=sent.senders(), broker_health_fn=lambda app: health)


DOWN = {"reachable": False, "error": "could not reach the broker: refused", "transport": "redis"}
UP = {"reachable": True, "transport": "redis", "details": redis_details(redis_info())}


def test_broker_unreachable_fires_only_after_the_minutes_and_resolves(env):
    _, sf = env
    sent = Sent()
    _rule(sf, "broker_unreachable", params={"minutes": 2})
    _run(env, sent, DOWN, NOW)
    _run(env, sent, DOWN, NOW + timedelta(minutes=1))
    assert sent.events() == []  # down, but not for long enough yet
    _run(env, sent, DOWN, NOW + timedelta(minutes=2))
    assert sent.events() == [("firing", "broker")]
    assert "unreachable for 2 min" in sent.messages[0].summary
    assert sent.messages[0].details["threshold_minutes"] == 2
    _run(env, sent, DOWN, NOW + timedelta(minutes=3))
    assert len(sent.messages) == 1  # one incident, one message
    _run(env, sent, UP, NOW + timedelta(minutes=4))
    assert sent.events() == [("firing", "broker"), ("resolved", "broker")]
    assert sent.messages[-1].summary == "The broker is reachable again"


def test_broker_blip_shorter_than_the_threshold_never_alerts_and_resets_the_clock(env):
    _, sf = env
    sent = Sent()
    _rule(sf, "broker_unreachable", params={"minutes": 2})
    _run(env, sent, DOWN, NOW)
    _run(env, sent, UP, NOW + timedelta(minutes=1))
    _run(env, sent, DOWN, NOW + timedelta(minutes=2))  # a new outage, clock restarts
    _run(env, sent, DOWN, NOW + timedelta(minutes=3))
    assert sent.events() == []
    _run(env, sent, DOWN, NOW + timedelta(minutes=4))
    assert sent.events() == [("firing", "broker")]


def _memory_health(**info):
    return {"reachable": True, "transport": "redis", "details": redis_details(redis_info(**info))}


def test_broker_memory_fires_at_the_ratio_and_resolves(env):
    _, sf = env
    sent = Sent()
    _rule(sf, "broker_memory", params={"ratio": 0.8})
    _run(env, sent, _memory_health(used_memory=79_000_000), NOW)
    assert sent.events() == []
    _run(env, sent, _memory_health(used_memory=85_000_000), NOW + timedelta(minutes=1))
    assert sent.events() == [("firing", "broker")]
    assert "85%" in sent.messages[0].summary
    _run(env, sent, _memory_health(used_memory=10_000_000), NOW + timedelta(minutes=2))
    assert sent.events()[-1] == ("resolved", "broker")


def test_broker_memory_never_fires_when_maxmemory_is_zero(env):
    _, sf = env
    sent = Sent()
    rule = _rule(sf, "broker_memory", params={"ratio": 0.01})
    _run(env, sent, _memory_health(maxmemory=0, used_memory=9_000_000_000), NOW)
    assert sent.events() == []
    with sf() as session:
        assert session.get(AlertRule, rule["id"]).last_check_error is None


def test_broker_memory_cant_be_judged_on_other_transports_or_when_down(env):
    _, sf = env
    sent = Sent()
    rule = _rule(sf, "broker_memory")
    _run(env, sent, {"reachable": True, "transport": "amqp", "details": {"product": "RabbitMQ"}}, NOW)
    with sf() as session:
        assert "only checked on Redis" in session.get(AlertRule, rule["id"]).last_check_error
    _run(env, sent, DOWN, NOW + timedelta(minutes=1))
    with sf() as session:
        assert "refused" in session.get(AlertRule, rule["id"]).last_check_error
    assert sent.events() == []
    with sf() as session:
        assert session.query(AlertIncident).count() == 0


def test_broker_rules_are_pinned_to_target_star(env):
    _, sf = env
    rule = _rule(sf, "broker_memory", target="redis-2")
    assert rule["target"] == "*"
    with sf() as session:
        other = alerts_control.create_rule(session, "admin", {"name": "q", "kind": "queue_backlog", "target": "celery"})
        moved = alerts_control.update_rule(session, "admin", other["id"], {"kind": "broker_unreachable"})
    assert moved["target"] == "*" and moved["params"] == {"minutes": 2}


def test_broker_param_validation_and_defaults(env):
    assert alerts_control.validate_params("broker_unreachable", {}) == {"minutes": 2}
    assert alerts_control.validate_params("broker_memory", {}) == {"ratio": 0.8}
    with pytest.raises(ValueError):
        alerts_control.validate_params("broker_memory", {"ratio": 1.5})
    with pytest.raises(ValueError):
        alerts_control.validate_params("broker_unreachable", {"ratio": 0.5})


# --- integration (real brokers) -----------------------------------------------------------------


@pytest.mark.skipif(not os.environ.get("CM_TEST_REDIS_URL"), reason="set CM_TEST_REDIS_URL to run against a real Redis")
def test_real_redis_health():
    url = os.environ["CM_TEST_REDIS_URL"]
    health = broker_health(Celery("t", broker=url))
    assert health["reachable"] is True and health["transport"] == "redis"
    d = health["details"]
    assert d["version"] and d["used_memory"] > 0
    assert d["maxmemory"] >= 0 and d["connected_clients"] >= 1
    assert d["maxmemory_policy"] in {
        "noeviction", "allkeys-lru", "allkeys-lfu", "allkeys-random", "volatile-lru", "volatile-lfu",
        "volatile-random", "volatile-ttl",
    }
    assert d["persistence"]["rdb_last_save_status"] in {"ok", "err", None}
    assert health["latency_ms"] > 0


@pytest.mark.skipif(not os.environ.get("CM_TEST_REDIS_URL"), reason="set CM_TEST_REDIS_URL to run against a real Redis")
def test_real_redis_flags_an_eviction_policy_and_a_limit():
    import redis

    url = os.environ["CM_TEST_REDIS_URL"]
    client = redis.Redis.from_url(url)
    old = {k: client.config_get(k)[k] for k in ("maxmemory", "maxmemory-policy")}
    try:
        client.config_set("maxmemory", 512 * 1024 * 1024)
        client.config_set("maxmemory-policy", "allkeys-lru")
        d = broker_health(Celery("t", broker=url))["details"]
        assert d["maxmemory"] == 512 * 1024 * 1024
        assert 0 < d["memory_ratio"] < 1
        assert d["maxmemory_policy"] == "allkeys-lru"
        assert any("allkeys-lru" in w and "queued tasks" in w for w in d["warnings"])
    finally:
        for key, value in old.items():
            client.config_set(key, value)


@pytest.mark.skipif(not os.environ.get("CM_TEST_REDIS_URL"), reason="set CM_TEST_REDIS_URL to run against a real Redis")
def test_real_redis_api_and_metrics_end_to_end():
    url = os.environ["CM_TEST_REDIS_URL"]
    client = _client(broker=url)
    client.post("/api/auth/login", json={"username": "bob", "password": "pw"})
    body = client.get("/api/broker/health").json()
    assert body["reachable"] and body["details"]["maxmemory_policy"]
    config = CeleryManagerConfig(celery_app=Celery("t", broker=url), db_url="sqlite:///:memory:", secret_key="s")
    _, session_factory = create_app(config)
    names = {f.name: f for f in compute_snapshot(config, session_factory)}
    assert names["celery_manager_broker_up"].samples == [({"transport": "redis"}, 1)]
    assert names["celery_manager_broker_memory_used_bytes"].samples[0][1] > 0


@pytest.mark.skipif(not os.environ.get("CM_TEST_AMQP_URL"), reason="set CM_TEST_AMQP_URL to run against a real RabbitMQ")
def test_real_rabbitmq_health():
    health = broker_health(Celery("t", broker=os.environ["CM_TEST_AMQP_URL"]))
    assert health["reachable"] is True and health["transport"] == "amqp"
    assert health["details"]["product"] == "RabbitMQ"
    assert health["details"]["version"]
    assert health["details"]["management"] == "not configured"
    assert health["latency_ms"] > 0


@pytest.mark.skipif(not os.environ.get("CM_TEST_AMQP_URL"), reason="set CM_TEST_AMQP_URL to run against a real RabbitMQ")
def test_real_rabbitmq_unreachable_port_reports_error():
    health = broker_health(Celery("t", broker=f"amqp://guest:{PASSWORD}@127.0.0.1:1//"))
    assert health["reachable"] is False and PASSWORD not in json.dumps(health)
