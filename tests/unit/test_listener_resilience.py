import contextlib
import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.control.health import listener_health
from celery_manager.db.models import AppSetting, Base, TaskRun
from celery_manager.events import listener as listener_module
from celery_manager.events.listener import EventListener, ListenerGaveUp
from celery_manager.events.liveness import (
    HEARTBEAT_KEY,
    ListenerStats,
    _serve_probe,
    write_heartbeat,
)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'l.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def sent(i):
    return {"type": "task-sent", "uuid": f"t{i}", "name": "demo.echo", "timestamp": datetime.now(timezone.utc).timestamp()}


def fake_app(scripts):
    """Each Receiver.capture() call plays the next script: events to deliver,
    optionally ending in an exception to raise (a dropped broker connection)."""
    queue = list(scripts)

    class Receiver:
        should_stop = False

        def __init__(self, connection, handlers):
            self.handler = handlers["*"]

        def capture(self, **kwargs):
            for item in queue.pop(0):
                if isinstance(item, Exception):
                    raise item
                self.handler(item)
            self.should_stop = True  # a clean return, as after SIGTERM

    events = SimpleNamespace(State=lambda: SimpleNamespace(event=lambda e: None), Receiver=Receiver)
    return SimpleNamespace(events=events, connection=lambda: contextlib.nullcontext())


def config(app, **extra):
    return SimpleNamespace(celery_app=app, worker_heartbeat_retention_days=0, store_results=True,
                           listener_reconnect_max_seconds=1, listener_max_consecutive_errors=50, **extra)


def test_survives_broker_restart_and_one_failed_write(sessions, monkeypatch):
    real = listener_module.upsert_task_run

    def flaky(session, event, config=None, stats=None):
        if event["uuid"] == "t3":
            raise RuntimeError("write failed")
        real(session, event, config, stats)

    monkeypatch.setattr(listener_module, "upsert_task_run", flaky)
    # One connection delivers 5 events (one fails to store) then the broker
    # drops; the reconnect delivers 5 more.
    app = fake_app([[sent(i) for i in range(5)] + [ConnectionError("broker restarted")],
                    [sent(i) for i in range(5, 10)]])
    EventListener(config(app), sessions).run(enable_events=False)
    with sessions() as session:
        ids = {r.task_id for r in session.query(TaskRun).all()}
        data = json.loads(session.get(AppSetting, HEARTBEAT_KEY).value)
        health = listener_health(session)
    assert ids == {f"t{i}" for i in range(10)} - {"t3"}
    assert (data["reconnects"], data["events_failed"], data["events_processed"]) == (1, 1, 9)
    assert health["state"] == "healthy" and health["reconnects"] == 1 and health["events_failed"] == 1


def test_gives_up_after_consecutive_failures(sessions, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(listener_module, "upsert_task_run", broken)
    cfg = config(fake_app([[sent(i) for i in range(5)]]))
    cfg.listener_max_consecutive_errors = 3
    with pytest.raises(ListenerGaveUp):
        EventListener(cfg, sessions).run(enable_events=False)
    with sessions() as session:
        assert json.loads(session.get(AppSetting, HEARTBEAT_KEY).value)["events_failed"] == 3


def test_concurrent_heartbeat_writers_share_one_durable_row(sessions):
    from celery_manager.events.liveness import write_heartbeat

    stats = ListenerStats()
    start = threading.Barrier(12)

    def write(_):
        start.wait(timeout=5)
        write_heartbeat(sessions, stats)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(12)))

    with sessions() as session:
        rows = session.query(AppSetting).filter_by(key=HEARTBEAT_KEY).all()
        assert len(rows) == 1
        assert json.loads(rows[0].value)["instance_id"] == stats.instance_id


def _beat(session, now, **fields):
    stats = ListenerStats()
    for key, value in fields.items():
        setattr(stats, key, value)
    write_heartbeat(lambda: contextlib.nullcontext(session), stats)
    row = session.get(AppSetting, HEARTBEAT_KEY)
    row.value = json.dumps({**json.loads(row.value), "at": now.isoformat()})
    session.commit()


def test_listener_health_states(sessions):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    with sessions() as session:
        assert listener_health(session, now=now)["state"] == "unknown"
        _beat(session, now, last_event_at=now - timedelta(seconds=5))
        assert listener_health(session, now=now)["state"] == "healthy"
        assert listener_health(session, now=now + timedelta(seconds=300))["state"] == "stale"  # no heartbeat
        _beat(session, now, last_event_at=now - timedelta(seconds=500))
        assert listener_health(session, now=now)["state"] == "stale"  # alive but receives nothing
        session.get(AppSetting, HEARTBEAT_KEY).value = "not json"
        assert listener_health(session, now=now)["state"] == "unknown"


def test_probe_port_reports_200_then_503():
    stats = ListenerStats()
    server = _serve_probe(0, stats)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        assert urllib.request.urlopen(url).status == 200
        stats.last_event_mono -= 1000
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url)
        assert caught.value.code == 503
    finally:
        server.shutdown()


def test_health_file_is_touched(sessions, tmp_path):
    from celery_manager.events.liveness import start_heartbeat
    stop = threading.Event()
    path = tmp_path / "alive"
    start_heartbeat(sessions, ListenerStats(), stop, health_file=str(path))
    for _ in range(100):
        if path.exists():
            break
        stop.wait(0.05)
    stop.set()
    assert path.exists()


def test_listener_stale_alert_and_metrics(sessions):
    from celery_manager.alerts.depth import check
    from celery_manager.alerts.evaluator import CheckUnavailable
    from celery_manager.metrics import METRIC_NAMES, _listener_families

    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    rule = SimpleNamespace(kind="listener_stale", target="*")
    with sessions() as session:
        probe = SimpleNamespace(session=session, now=now)
        with pytest.raises(CheckUnavailable):
            check(rule, {"seconds": 120}, probe)
        assert _listener_families(session, now) == []
        _beat(session, now, last_event_at=now - timedelta(seconds=2), lag_seconds=1.5, failed=4)
        assert not check(rule, {"seconds": 120}, probe).firing
        later = SimpleNamespace(session=session, now=now + timedelta(hours=1))
        assert "listener" in check(rule, {"seconds": 120}, later).firing
        families = {f.name: f.samples for f in _listener_families(session, now)}
    assert set(families) <= set(METRIC_NAMES)
    assert families["celery_manager_listener_events_failed"] == [({}, 4)]
    assert families["celery_manager_listener_lag_seconds"] == [({}, 1.5)]
