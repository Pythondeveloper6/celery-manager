"""Queue wait time and queue depth history: listener timestamps, wait math,
percentiles, the sampler, pruning, history downsampling, the growth rule, both
alert kinds, the API and metrics, and migration 0011."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from celery import Celery
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from celery_manager.alerts import evaluate_once
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import alerts as alert_control
from celery_manager.control import depth_history
from celery_manager.control.depth_history import growth, history, prune_samples, sample_once
from celery_manager.control.latency import percentile, wait_seconds, wait_stats
from celery_manager.control.retention import run_prune
from celery_manager.db.migrate import alembic_config, upgrade_database
from celery_manager.db.models import AlertIncident, Base, QueueDepthSample, TaskRun
from celery_manager.db.session import get_session_factory, init_db
from celery_manager.events.listener import upsert_task_run
from celery_manager.metrics import compute_snapshot

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def ts(dt: datetime) -> float:
    return dt.timestamp()


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def env(tmp_path):
    config = CeleryManagerConfig(
        celery_app=Celery("t"), db_url=f"sqlite:///{tmp_path / 'cm.db'}", secret_key="s",
        dashboard_url="https://ops.example.com/cm/",
    )
    engine = init_db(config)
    yield config, get_session_factory(engine)
    engine.dispose()


# --- listener: sent_at / received_at / eta -----------------------------------------------


def test_listener_stores_sent_and_received_timestamps(session):
    upsert_task_run(session, {"type": "task-sent", "uuid": "a", "name": "t", "timestamp": ts(NOW)})
    upsert_task_run(session, {"type": "task-received", "uuid": "a", "name": "t", "timestamp": ts(NOW) + 2})
    session.commit()
    run = session.query(TaskRun).one()
    assert run.sent_at.replace(tzinfo=timezone.utc) == NOW
    assert run.received_at.replace(tzinfo=timezone.utc) == NOW + timedelta(seconds=2)


def test_listener_never_replaces_an_earlier_time_with_a_later_one(session):
    upsert_task_run(session, {"type": "task-sent", "uuid": "a", "name": "t", "timestamp": ts(NOW)})
    session.commit()
    # A duplicate / redelivered event, and an event from a clock that ran late.
    upsert_task_run(session, {"type": "task-sent", "uuid": "a", "name": "t", "timestamp": ts(NOW) + 30})
    session.commit()
    assert session.query(TaskRun).one().sent_at.replace(tzinfo=timezone.utc) == NOW
    # An earlier one does replace (events can be processed out of order).
    upsert_task_run(session, {"type": "task-sent", "uuid": "a", "name": "t", "timestamp": ts(NOW) - 5})
    session.commit()
    assert session.query(TaskRun).one().sent_at.replace(tzinfo=timezone.utc) == NOW - timedelta(seconds=5)


def test_a_late_task_sent_still_records_its_time_after_the_run_started(session):
    upsert_task_run(session, {"type": "task-started", "uuid": "a", "name": "t"})
    upsert_task_run(session, {"type": "task-sent", "uuid": "a", "name": "t", "timestamp": ts(NOW)})
    session.commit()
    run = session.query(TaskRun).one()
    assert run.status == "started"  # the stale status is still refused
    assert run.sent_at is not None


def test_events_without_a_timestamp_leave_the_columns_alone(session):
    upsert_task_run(session, {"type": "task-sent", "uuid": "a", "name": "t"})
    upsert_task_run(session, {"type": "task-received", "uuid": "a", "name": "t", "timestamp": "garbage"})
    session.commit()
    run = session.query(TaskRun).one()
    assert run.sent_at is None and run.received_at is None


def test_listener_records_eta_and_a_retry_forgets_the_old_attempt(session):
    eta = NOW + timedelta(minutes=5)
    upsert_task_run(session, {"type": "task-sent", "uuid": "a", "name": "t", "timestamp": ts(NOW),
                              "eta": eta.isoformat()})
    session.commit()
    run = session.query(TaskRun).one()
    assert run.eta.replace(tzinfo=timezone.utc) == eta
    upsert_task_run(session, {"type": "task-started", "uuid": "a"})
    upsert_task_run(session, {"type": "task-retried", "uuid": "a"})
    session.commit()
    run = session.query(TaskRun).one()
    assert run.sent_at is None and run.received_at is None and run.eta is None


# --- wait math ----------------------------------------------------------------------------


def test_wait_is_start_minus_send_without_eta():
    assert wait_seconds(NOW, None, NOW + timedelta(seconds=7)) == 7


def test_wait_excludes_eta_time():
    # Sent at NOW with a 5 minute countdown, started 2s after it was due.
    eta = NOW + timedelta(minutes=5)
    assert wait_seconds(NOW, eta, eta + timedelta(seconds=2)) == 2
    # An ETA already in the past at send time changes nothing.
    assert wait_seconds(NOW, NOW - timedelta(minutes=1), NOW + timedelta(seconds=3)) == 3


def test_wait_needs_a_send_time_and_never_goes_negative():
    assert wait_seconds(None, None, NOW) is None
    assert wait_seconds(NOW, None, None) is None
    assert wait_seconds(NOW, None, NOW - timedelta(seconds=1)) == 0.0  # clock skew


def test_wait_handles_naive_datetimes_from_sqlite():
    assert wait_seconds(NOW.replace(tzinfo=None), None, (NOW + timedelta(seconds=4)).replace(tzinfo=None)) == 4


def test_percentile_interpolates():
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 50) == 2.5
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 4.0
    assert percentile([5.0], 95) == 5.0
    assert percentile(list(map(float, range(1, 101))), 95) == pytest.approx(95.05)
    with pytest.raises(ValueError):
        percentile([], 50)


def _run(session, task_id, queue, name, wait, *, started_ago=60, eta_after=None, sent=True):
    started = NOW - timedelta(seconds=started_ago)
    session.add(TaskRun(
        task_id=task_id, task_name=name, status="succeeded", queue=queue,
        sent_at=started - timedelta(seconds=wait + (eta_after or 0)) if sent else None,
        eta=started - timedelta(seconds=wait) if eta_after else None,
        started_at=started,
    ))


def test_wait_stats_per_queue_and_task_over_the_window(session):
    for i, w in enumerate([1, 2, 3, 4, 10]):
        _run(session, f"a{i}", "fast", "demo.a", w)
    _run(session, "b0", "slow", "demo.b", 100)
    _run(session, "b1", "slow", "demo.b", 200, eta_after=3600)  # ETA hour excluded
    _run(session, "nosent", "fast", "demo.a", 999, sent=False)  # no send time: left out
    _run(session, "old", "fast", "demo.a", 999, started_ago=3600)  # outside the window
    _run(session, "noqueue", None, "demo.c", 5)
    session.commit()
    stats = wait_stats(session, now=NOW)
    assert stats["window_seconds"] == 900 and not stats["truncated"]
    fast = stats["by_queue"]["fast"]
    assert fast["runs"] == 5 and fast["p50"] == 3 and fast["max"] == 10
    assert fast["p95"] == pytest.approx(percentile([1, 2, 3, 4, 10], 95))
    assert stats["by_queue"]["slow"]["max"] == pytest.approx(200)
    assert stats["by_task"]["demo.c"]["runs"] == 1
    assert "demo.c" not in {q for q in stats["by_queue"]}
    assert stats["overall"]["runs"] == 8


def test_wait_stats_row_cap_truncates_to_newest(session):
    for i in range(5):
        _run(session, f"r{i}", "q", "t", 1, started_ago=10 + i)
    session.commit()
    stats = wait_stats(session, now=NOW, row_cap=3)
    assert stats["truncated"] and stats["by_queue"]["q"]["runs"] == 3


# --- sampler ----------------------------------------------------------------------------


def fake_listing(depths, **extra):
    def fake(celery_app, session, include_workers=True):
        assert include_workers is False
        return {"broker": {"error": None}, "queues": [{"name": n, "depth": d, **extra} for n, d in depths.items()]}
    return fake


def test_sampler_stores_known_depths_and_skips_unknown(env):
    config, sf = env
    written = sample_once(sf, config.celery_app, now=NOW, list_queues_fn=fake_listing({"a": 5, "b": None, "c": 0}))
    assert written == 2
    with sf() as s:
        rows = {r.queue: r.depth for r in s.query(QueueDepthSample).all()}
    assert rows == {"a": 5, "c": 0}


def test_sampler_skips_undeclared_rabbitmq_queues_but_keeps_empty_redis_ones(env):
    config, sf = env

    def listing(celery_app, session, include_workers=True):
        return {"broker": {"error": None}, "queues": [
            {"name": "amqp-undeclared", "depth": 0, "exists": False, "reason": "not declared on the broker yet"},
            {"name": "redis-empty", "depth": 0, "exists": False, "reason": None},
        ]}

    sample_once(sf, config.celery_app, now=NOW, list_queues_fn=listing)
    with sf() as s:
        assert [r.queue for r in s.query(QueueDepthSample).all()] == ["redis-empty"]


def test_sampler_writes_nothing_when_the_broker_is_down(env):
    config, sf = env

    def down(celery_app, session, include_workers=True):
        return {"broker": {"error": "timed out"}, "queues": [{"name": "a", "depth": None}]}

    assert sample_once(sf, config.celery_app, now=NOW, list_queues_fn=down) == 0


def test_sampler_dedupes_per_interval_across_processes(env):
    config, sf = env
    listing = fake_listing({"a": 1})
    assert sample_once(sf, config.celery_app, interval=60, now=NOW, list_queues_fn=listing) == 1
    # Another listener ticks 20s later: it sees a fresh sample and skips.
    assert sample_once(sf, config.celery_app, interval=60, now=NOW + timedelta(seconds=20), list_queues_fn=listing) == 0
    # By the next interval it is due again.
    assert sample_once(sf, config.celery_app, interval=60, now=NOW + timedelta(seconds=60), list_queues_fn=listing) == 1


def test_sampler_thread_is_off_at_zero_interval(env):
    config, sf = env
    config.queue_sample_interval_seconds = 0
    assert depth_history.start_in_background(sf, config.celery_app, config) is None


def test_sampler_thread_samples_and_stops(env, monkeypatch):
    config, sf = env
    config.queue_sample_interval_seconds = 0.05
    monkeypatch.setattr("celery_manager.control.queues.list_queues", fake_listing({"a": 9}))
    thread, stop = depth_history.start_in_background(sf, config.celery_app, config)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with sf() as s:
                if s.query(QueueDepthSample).count():
                    break
            time.sleep(0.02)
        else:
            pytest.fail("the sampler never wrote a sample")
    finally:
        stop.set()
        thread.join(2)
    assert not thread.is_alive()


# --- retention ---------------------------------------------------------------------------


def _samples(session, queue, series, end=NOW, step=60):
    n = len(series)
    for i, depth in enumerate(series):
        session.add(QueueDepthSample(sampled_at=end - timedelta(seconds=step * (n - 1 - i)), queue=queue, depth=depth))
    session.commit()


def test_prune_samples_deletes_only_old_rows(session):
    session.add(QueueDepthSample(sampled_at=NOW - timedelta(days=15), queue="q", depth=1))
    session.add(QueueDepthSample(sampled_at=NOW - timedelta(days=13), queue="q", depth=2))
    session.commit()
    assert prune_samples(session, 14, now=NOW, dry_run=True) == 1
    assert session.query(QueueDepthSample).count() == 2
    assert prune_samples(session, 14, now=NOW) == 1
    assert [r.depth for r in session.query(QueueDepthSample)] == [2]
    assert prune_samples(session, 0, now=NOW) == 0  # 0 keeps forever


def test_run_prune_includes_samples_and_honours_the_setting(session):
    session.add(QueueDepthSample(sampled_at=NOW - timedelta(days=20), queue="q", depth=1))
    session.add(QueueDepthSample(sampled_at=NOW - timedelta(days=2), queue="q", depth=2))
    session.commit()
    preview = run_prune(session, now=NOW, dry_run=True)
    assert preview["samples_deleted"] == 1 and session.query(QueueDepthSample).count() == 2
    assert run_prune(session, now=NOW, sample_retention_days=1)["samples_deleted"] == 2
    session.add(QueueDepthSample(sampled_at=NOW - timedelta(days=400), queue="q", depth=3))
    session.commit()
    assert run_prune(session, now=NOW, sample_retention_days=None)["samples_deleted"] == 0
    assert run_prune(session, now=NOW)["samples_deleted"] == 1  # default 14 days


def test_the_sampler_thread_prunes_by_itself(env, monkeypatch):
    config, sf = env
    with sf() as s:
        s.add(QueueDepthSample(sampled_at=datetime.now(timezone.utc) - timedelta(days=30), queue="old", depth=1))
        s.commit()
    config.queue_sample_interval_seconds = 0.05
    monkeypatch.setattr("celery_manager.control.queues.list_queues", fake_listing({"a": 9}))
    thread, stop = depth_history.start_in_background(sf, config.celery_app, config)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with sf() as s:
                if not s.query(QueueDepthSample).filter_by(queue="old").count():
                    break
            time.sleep(0.02)
        else:
            pytest.fail("the sampler never pruned the old sample")
    finally:
        stop.set()
        thread.join(2)


# --- history downsampling ---------------------------------------------------------------


def test_history_returns_points_in_order_and_keeps_peaks(session):
    _samples(session, "q", [1, 2, 50, 3, 4], step=60)
    out = history(session, "q", hours=1, now=NOW)
    assert out["samples"] == 5
    # Under max_points, so one bucket per sample minute and nothing is lost.
    assert [p["depth"] for p in out["points"]] == [1, 2, 50, 3, 4]
    assert out["points"] == sorted(out["points"], key=lambda p: p["at"])


def test_history_downsamples_to_bucket_maxima(session):
    _samples(session, "q", list(range(600)), step=60)  # ten hours of minutes
    out = history(session, "q", hours=10, now=NOW, max_points=20)
    assert out["bucket_seconds"] == 1800
    assert len(out["points"]) <= 21
    # Each bucket reports its highest depth; the final one holds the newest.
    assert out["points"][-1]["depth"] == 599
    assert [p["depth"] for p in out["points"]] == sorted(p["depth"] for p in out["points"])


def test_history_is_per_queue_windowed_and_gappy_when_unsampled(session):
    _samples(session, "q", [5], end=NOW - timedelta(hours=30))
    _samples(session, "q", [7], end=NOW - timedelta(hours=1))
    _samples(session, "other", [9])
    out = history(session, "q", hours=24, now=NOW)
    assert [p["depth"] for p in out["points"]] == [7]
    assert history(session, "never", hours=24, now=NOW)["points"] == []


# --- growth rule --------------------------------------------------------------------------


def test_growth_fires_on_a_steady_climb():
    assert growth([0, 10, 25, 40, 60], 50)["increase"] == 60


def test_growth_tolerates_a_small_dip_but_not_a_drain():
    assert growth([0, 20, 15, 40, 60, 80, 100, 120, 140, 160], 50) is not None  # 1 of 9 steps down, small
    assert growth([0, 100, 200, 90, 220], 50) is None  # 1 of 4 steps down: over 20%
    assert growth([0, 50, 100, 150, 200, 250, 300, 50, 300], 50) is None  # drained by more than half the rise
    assert growth([0, 20, 10, 30, 20, 40], 20) is None  # sawtooth: 2 of 5 steps down


def test_growth_needs_enough_rise_and_samples():
    assert growth([10, 20, 30], 50) is None
    assert growth([0, 100], 50) is None
    assert growth([100, 100, 100, 100], 1) is None  # flat
    assert growth([100, 90, 80, 70], 1) is None  # draining


def test_growth_ignores_flat_steps():
    assert growth([0, 0, 0, 30, 30, 60, 60], 50) is not None


# --- alerts -------------------------------------------------------------------------------


class Fake:
    def __init__(self):
        self.sent = []

    def __call__(self, channel_config, message, config):
        self.sent.append((message.event, message.key, message.summary))


def _alert_rule(sf, kind, **fields):
    with sf() as s:
        channel = alert_control.create_channel(s, "admin", "ops", "webhook", {"url": "https://example.com/x"})
        return alert_control.create_rule(
            s, "admin", {"name": f"{kind} rule", "kind": kind, "channel_ids": [channel["id"]], **fields}
        )


def _evaluate(env, fake, now=NOW):
    config, sf = env
    senders = {"slack": fake, "webhook": fake, "email": fake}
    return evaluate_once(sf, config.celery_app, config, now, senders=senders)


def test_queue_wait_defaults_and_validation():
    clean = alert_control.validate_params("queue_wait", {})
    assert clean == {"seconds": 60.0, "quantile": 95, "window_minutes": 15, "min_runs": 5}
    assert alert_control.validate_params("queue_growing", {}) == {"minutes": 15, "min_increase": 50}
    with pytest.raises(ValueError):
        alert_control.validate_params("queue_wait", {"quantile": 0})
    with pytest.raises(ValueError):
        alert_control.validate_params("queue_wait", {"threshold": 5})


def test_queue_wait_alert_fires_per_queue_and_resolves(env):
    config, sf = env
    fake = Fake()
    _alert_rule(sf, "queue_wait", params={"seconds": 30, "min_runs": 3})
    with sf() as s:
        for i in range(5):
            _run(s, f"slow{i}", "slow", "t", 120)
        for i in range(5):
            _run(s, f"ok{i}", "ok", "t", 1)
        for i in range(2):  # too few runs to judge
            _run(s, f"few{i}", "few", "t", 500)
        s.commit()
    _evaluate(env, fake)
    assert [(e, k) for e, k, _ in fake.sent] == [("firing", "slow")]
    assert "p95 wait is" in fake.sent[0][2]
    # The slow runs age out of the 15 minute window: the incident resolves.
    _evaluate(env, fake, NOW + timedelta(minutes=20))
    assert [(e, k) for e, k, _ in fake.sent][-1] == ("resolved", "slow")


def test_queue_wait_alert_ignores_eta_time(env):
    config, sf = env
    fake = Fake()
    _alert_rule(sf, "queue_wait", params={"seconds": 30, "min_runs": 3})
    with sf() as s:
        for i in range(5):  # 1h of countdown, 2s of real waiting
            _run(s, f"eta{i}", "q", "t", 2, eta_after=3600)
        s.commit()
    _evaluate(env, fake)
    assert fake.sent == []


def _depth_rows(sf, queue, series, end=NOW, step=60):
    with sf() as s:
        _samples(s, queue, series, end=end, step=step)


def test_queue_growing_fires_and_resolves(env):
    config, sf = env
    fake = Fake()
    _alert_rule(sf, "queue_growing", params={"minutes": 10, "min_increase": 50})
    _depth_rows(sf, "busy", [0, 10, 25, 40, 60, 80, 100, 130, 160, 190, 220])  # 11 samples, 10 min
    _depth_rows(sf, "calm", [5, 6, 5, 5, 6, 5, 5, 6, 5, 5, 5])
    _evaluate(env, fake)
    assert [(e, k) for e, k, _ in fake.sent] == [("firing", "busy")]
    assert "grew from 0 to 220" in fake.sent[0][2]
    # It drains: new samples show a falling queue.
    later = NOW + timedelta(minutes=10)
    _depth_rows(sf, "busy", [200, 150, 90, 40, 10, 5, 3, 2, 1, 0, 0], end=later)
    _evaluate(env, fake, later)
    assert [(e, k) for e, k, _ in fake.sent][-1] == ("resolved", "busy")


def test_queue_growing_is_unknown_without_enough_fresh_samples(env):
    config, sf = env
    fake = Fake()
    rule = _alert_rule(sf, "queue_growing", params={"minutes": 10, "min_increase": 50})
    _depth_rows(sf, "busy", [0, 10, 25, 40, 60, 80, 100, 130, 160, 190, 220])
    _evaluate(env, fake)
    # Sampling stops: 8 minutes on, only stale readings remain in the window.
    _evaluate(env, fake, NOW + timedelta(minutes=8))
    assert [(e, k) for e, k, _ in fake.sent] == [("firing", "busy")]
    with sf() as s:
        assert s.query(AlertIncident).filter_by(rule_id=rule["id"]).one().state == "firing"


def test_queue_growing_says_so_when_nothing_was_ever_sampled(env):
    config, sf = env
    fake = Fake()
    rule = _alert_rule(sf, "queue_growing")
    _evaluate(env, fake)
    with sf() as s:
        from celery_manager.db.models import AlertRule
        assert "no queue depth samples" in s.get(AlertRule, rule["id"]).last_check_error


# --- API, metrics, roles ----------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    config = CeleryManagerConfig(
        celery_app=Celery("t"), db_url="sqlite:///:memory:", secret_key="s", metrics_token="tok",
        metrics_cache_seconds=0, inspect_timeout_seconds=0.01,
    )
    app, sf = create_app(config)
    with sf() as s:
        create_user(s, "viewer", "pw-long-enough", role="viewer")
    client = TestClient(app, base_url="https://testserver")
    return client, sf, config


def _login(client):
    assert client.post("/api/auth/login", json={"username": "viewer", "password": "pw-long-enough"}).status_code == 200


def test_history_endpoint_is_readable_by_a_viewer_and_needs_a_session(api):
    client, sf, _ = api
    assert client.get("/api/queues/celery/history").status_code == 401
    _login(client)
    now = datetime.now(timezone.utc)
    with sf() as s:
        _samples(s, "celery", [1, 4, 2], end=now)
    body = client.get("/api/queues/celery/history", params={"hours": 2}).json()
    assert body["queue"] == "celery" and [p["depth"] for p in body["points"]] == [1, 4, 2]
    assert client.get("/api/queues/unknown/history").json()["points"] == []
    assert client.get("/api/queues/celery/history", params={"hours": 0}).status_code == 422
    assert client.get("/api/queues/celery/history", params={"hours": 100000}).status_code == 422


def test_queue_listing_and_task_detail_carry_wait(api, monkeypatch):
    client, sf, _ = api
    _login(client)
    monkeypatch.setattr("celery_manager.api.routes.queues.list_queues", lambda *a, **k: {
        "broker": {"error": None}, "recorded_queued_unassigned": 0,
        "queues": [{"name": "q", "depth": 1}, {"name": "idle", "depth": 0}],
    })
    now = datetime.now(timezone.utc)
    with sf() as s:
        for i in range(3):
            s.add(TaskRun(task_id=f"r{i}", task_name="demo.a", status="succeeded", queue="q",
                          sent_at=now - timedelta(seconds=40), started_at=now - timedelta(seconds=30)))
        s.commit()
    listing = client.get("/api/queues").json()
    by_name = {q["name"]: q for q in listing["queues"]}
    assert by_name["q"]["wait"]["runs"] == 3 and by_name["q"]["wait"]["p95"] == pytest.approx(10, abs=0.01)
    assert by_name["idle"]["wait"] is None
    detail = client.get("/api/tasks/r0").json()
    assert detail["wait_seconds"] == pytest.approx(10, abs=0.01) and detail["sent_at"]
    stats = client.get("/api/analytics/tasks/demo.a").json()
    assert stats["wait"]["runs"] == 3


def test_metrics_expose_queue_wait_quantiles(api):
    client, sf, config = api
    now = datetime.now(timezone.utc)
    with sf() as s:
        for i, w in enumerate([1, 2, 3, 4, 10]):
            s.add(TaskRun(task_id=f"m{i}", task_name="demo.a", status="succeeded", queue="q",
                          sent_at=now - timedelta(seconds=60 + w), started_at=now - timedelta(seconds=60)))
        s.commit()
    snapshot = {f.name: f for f in compute_snapshot(config, sf)}
    samples = {(l["queue"], l["quantile"]): v for l, v in snapshot["celery_manager_queue_wait_seconds"].samples}
    assert samples[("q", "0.5")] == pytest.approx(3, abs=0.01)
    assert samples[("q", "1")] == pytest.approx(10, abs=0.01)
    assert ("q", "0.95") in samples


# --- migration ---------------------------------------------------------------------------------


def test_migration_0011_adds_columns_and_table_and_reverses(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cm.db'}")
    upgrade_database(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("task_runs")}
    assert {"sent_at", "received_at"} <= cols
    tables = inspect(engine).get_table_names()
    assert "queue_depth_samples" in tables
    indexes = inspect(engine).get_indexes("queue_depth_samples")
    assert [i["column_names"] for i in indexes] == [["queue", "sampled_at"]]
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "0010")
    cols = {c["name"] for c in inspect(engine).get_columns("task_runs")}
    assert not {"sent_at", "received_at"} & cols
    assert "queue_depth_samples" not in inspect(engine).get_table_names()
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "0011")
    assert "queue_depth_samples" in inspect(engine).get_table_names()
    engine.dispose()


# --- real broker (opt-in) ---------------------------------------------------------------------


@pytest.mark.skipif(not __import__("os").environ.get("CM_TEST_REDIS_URL"), reason="needs CM_TEST_REDIS_URL")
def test_sampler_records_the_real_redis_queue_depth(env):
    import os

    import redis

    url = os.environ["CM_TEST_REDIS_URL"]
    client = redis.Redis.from_url(url)
    queue = f"cm-depth-test-{threading.get_ident()}"
    client.delete(queue)
    try:
        for i in range(3):
            client.lpush(queue, f'{{"n": {i}}}')
        config, sf = env
        celery_app = Celery("t", broker=url)
        celery_app.conf.task_default_queue = queue
        written = sample_once(sf, celery_app, now=NOW)
        assert written >= 1
        with sf() as s:
            row = s.query(QueueDepthSample).filter_by(queue=queue).one()
        assert row.depth == 3
    finally:
        client.delete(queue)
