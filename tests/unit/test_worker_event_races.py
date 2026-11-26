"""Independent event listeners must converge on one ordered worker sighting."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from threading import Barrier

from celery_manager import CeleryManagerConfig
from celery_manager.db.models import WorkerHeartbeatSample, WorkerState
from celery_manager.db.session import get_session_factory, init_db
from celery_manager.events.workers import record_worker_event


def test_concurrent_first_sightings_keep_latest_event(tmp_path):
    engine = init_db(CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///{tmp_path / 'workers.db'}"))
    sessions = get_session_factory(engine)
    barrier = Barrier(8)

    def observe(index):
        with sessions() as session:
            barrier.wait(timeout=10)
            record_worker_event(session, {"type": "worker-offline" if index == 7 else "worker-heartbeat",
                                          "hostname": "same-worker", "timestamp": 1000 + index})
            session.commit()
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(observe, range(8)))
        with sessions() as session:
            row = session.query(WorkerState).one()
            assert row.state == "offline"
            assert row.last_seen_at.replace(tzinfo=timezone.utc).timestamp() == 1007
    finally:
        engine.dispose()


def test_stale_identity_map_does_not_overwrite_newer_sighting(tmp_path):
    engine = init_db(CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///{tmp_path / 'workers.db'}"))
    sessions = get_session_factory(engine)
    try:
        with sessions() as session:
            record_worker_event(session, {"type": "worker-online", "hostname": "worker", "timestamp": 1000})
            session.commit()
        with sessions() as stale:
            original = stale.get(WorkerState, "worker")
            stale.commit()  # retain the identity map while releasing its transaction
            with sessions() as fresh:
                record_worker_event(fresh, {"type": "worker-offline", "hostname": "worker", "timestamp": 1100})
                fresh.commit()
            assert original.state == "online"
            record_worker_event(stale, {"type": "worker-heartbeat", "hostname": "worker", "timestamp": 1050})
            stale.commit()
            assert original.state == "offline"
    finally:
        engine.dispose()


def test_invalid_worker_event_shapes_are_ignored(tmp_path):
    engine = init_db(CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///{tmp_path / 'workers.db'}"))
    sessions = get_session_factory(engine)
    try:
        with sessions() as session:
            for event in (
                {"type": "task-sent", "hostname": "worker", "timestamp": 1000},
                {"type": "worker-online", "hostname": "", "timestamp": 1000},
                {"type": "worker-heartbeat", "hostname": "x" * 256, "timestamp": 1000},
                {"type": "worker-heartbeat", "hostname": "bad-time", "timestamp": "not-a-time"},
            ):
                record_worker_event(session, event)
            session.commit()
            assert session.query(WorkerState).count() == 0
            assert session.query(WorkerHeartbeatSample).count() == 0
    finally:
        engine.dispose()


def test_worker_event_counters_and_samples_are_bounded_by_event_age(tmp_path):
    from datetime import datetime, timedelta

    engine = init_db(CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///{tmp_path / 'workers.db'}"))
    sessions = get_session_factory(engine)
    try:
        now = datetime.now(timezone.utc)
        with sessions() as session:
            record_worker_event(session, {"type": "worker-heartbeat", "hostname": "worker",
                                          "timestamp": (now - timedelta(days=3)).timestamp(),
                                          "active": True, "processed": -1}, retention_days=1)
            session.commit()
            row = session.get(WorkerState, "worker")
            assert row.active is None and row.processed is None
            assert session.query(WorkerHeartbeatSample).count() == 0

            recent = now.timestamp()
            record_worker_event(session, {"type": "worker-heartbeat", "hostname": "worker",
                                          "timestamp": recent, "active": 2, "processed": 50}, retention_days=1)
            session.commit()
            row = session.get(WorkerState, "worker")
            sample = session.query(WorkerHeartbeatSample).one()
            assert (row.active, row.processed, row.state) == (2, 50, "online")
            assert (sample.active, sample.processed, sample.state) == (2, 50, "online")
    finally:
        engine.dispose()
