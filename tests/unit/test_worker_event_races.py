"""Independent event listeners must converge on one ordered worker sighting."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from threading import Barrier

from celery_manager import CeleryManagerConfig
from celery_manager.db.models import WorkerState
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
