from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager import reconcile
from celery_manager.db.models import Base, TaskRun, WorkerState
from celery_manager.events.listener import upsert_task_run

OLD = datetime.now(timezone.utc) - timedelta(hours=1)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def make_config(app, **extra):
    return SimpleNamespace(celery_app=app, reconcile_after_seconds=120, reconcile_batch_size=20, result_max_bytes=64_000,
                           store_results=True, inspect_timeout_seconds=0.1, **extra)


class Backend:
    def __init__(self, metas):
        self.metas = metas

    def get_task_meta(self, task_id):
        return self.metas.get(task_id, {"status": "PENDING", "result": None})


def add_runs(sessions, n, status="started", prefix="t", **extra):
    with sessions() as session:
        for i in range(n):
            session.add(TaskRun(task_id=f"{prefix}{i}", task_name="demo.echo", status=status, updated_at=OLD, **extra))
        session.commit()


def test_open_runs_get_their_final_status_from_the_backend(sessions):
    add_runs(sessions, 50)
    metas = {f"t{i}": {"status": "SUCCESS", "result": i, "date_done": "2026-11-01T10:00:00+00:00"} for i in range(50)}
    config = make_config(SimpleNamespace(backend=Backend(metas)))
    counts = reconcile.reconcile_once(config, sessions)
    assert counts["checked"] == 50 and counts["from_backend"] == 50
    with sessions() as session:
        runs = session.query(TaskRun).all()
    assert {r.status for r in runs} == {"succeeded"} and {r.status_source for r in runs} == {"backend"}
    assert runs[0].result_repr is not None and runs[0].finished_at is not None


def test_failure_pending_and_recent_runs(sessions):
    add_runs(sessions, 1, prefix="bad")
    add_runs(sessions, 1, prefix="wait")
    with sessions() as session:
        session.add(TaskRun(task_id="fresh", task_name="x", status="started", updated_at=datetime.now(timezone.utc)))
        session.commit()
    metas = {"bad0": {"status": "FAILURE", "result": ValueError("boom"), "traceback": "tb"},
             "fresh": {"status": "SUCCESS", "result": 1}}
    reconcile.reconcile_once(make_config(SimpleNamespace(backend=Backend(metas))), sessions)
    with sessions() as session:
        by_id = {r.task_id: r for r in session.query(TaskRun).all()}
    assert by_id["bad0"].status == "failed" and "boom" in by_id["bad0"].exception and by_id["bad0"].traceback == "tb"
    assert by_id["wait0"].status == "started" and by_id["wait0"].status_source is None
    assert by_id["fresh"].status == "started"  # touched too recently to check


def test_no_result_backend_changes_nothing(sessions):
    from celery import Celery
    add_runs(sessions, 2)
    app = Celery("noback")
    assert reconcile.reconcile_once(make_config(app), sessions)["from_backend"] == 0


def test_lost_only_when_every_source_agrees(sessions, monkeypatch):
    add_runs(sessions, 2)
    config = make_config(SimpleNamespace(backend=Backend({})))
    monkeypatch.setattr(reconcile, "_lost_candidates_held", lambda config, session: None)
    assert reconcile.reconcile_once(config, sessions)["lost"] == 0
    monkeypatch.setattr(reconcile, "_lost_candidates_held", lambda config, session: {"t0"})
    assert reconcile.reconcile_once(config, sessions)["lost"] == 1
    with sessions() as session:
        run = session.query(TaskRun).filter_by(task_id="t1").one()
        assert (run.status, run.status_source) == ("lost", "reconcile") and run.exception
        # A later lifecycle event overwrites `lost` and clears the reason.
        upsert_task_run(session, {"type": "task-succeeded", "uuid": "t1", "runtime": 1.0})
        session.commit()
        assert (run.status, run.status_source, run.exception) == ("succeeded", None, None)


def inspector(ping, **replies):
    class Inspect:
        def __init__(self, destination=None, timeout=None, limit=None):
            pass

        def ping(self):
            return ping

        def active(self):
            return replies.get("active")

        def reserved(self):
            return replies.get("reserved")

        def scheduled(self):
            return replies.get("scheduled")

    return SimpleNamespace(control=SimpleNamespace(inspect=Inspect))


def test_held_task_ids_requires_every_expected_worker_to_answer():
    reply = {"w1": [{"id": "a"}]}
    app = inspector({"w1": {}}, active=reply, reserved={"w1": []}, scheduled={"w1": [{"request": {"id": "s"}}]})
    assert reconcile._held_task_ids(app, 1, {"w1"}) == {"a", "s"}
    assert reconcile._held_task_ids(app, 1, {"w1", "w2"}) is None  # w2 never answered
    assert reconcile._held_task_ids(inspector({}), 1, set()) is None  # nobody answered
    partial = inspector({"w1": {}}, active=reply, reserved={}, scheduled={"w1": []})
    assert reconcile._held_task_ids(partial, 1, set()) is None  # reserved reply missing


def test_seed_inflight_creates_missing_rows_only(sessions):
    add_runs(sessions, 1, prefix="known")
    app = inspector({}, active={"w1": [{"id": "new", "name": "demo.echo", "args": "[1]", "kwargs": "{}", "time_start": 1_700_000_000.0,
                                        "delivery_info": {"routing_key": "q1"}}, {"id": "known0", "name": "demo.echo"}]},
                    reserved={"w1": [{"id": "wait", "name": "demo.slow"}]})
    assert reconcile.seed_inflight(make_config(app), sessions) == 2
    with sessions() as session:
        by_id = {r.task_id: r for r in session.query(TaskRun).all()}
    assert by_id["new"].status == "started" and by_id["new"].status_source == "seed" and by_id["new"].queue == "q1"
    assert by_id["wait"].status == "queued" and by_id["known0"].status_source is None


def test_start_in_background_is_off_by_default():
    assert reconcile.start_in_background(SimpleNamespace(reconcile_interval_seconds=0), None) is None


def test_only_recently_seen_online_workers_are_expected(sessions, monkeypatch):
    monkeypatch.setattr(reconcile, "_broker_task_ids", lambda app, session, timeout: {"b"})
    app = inspector({"w1": {}}, active={"w1": [{"id": "a"}]}, reserved={"w1": []}, scheduled={"w1": []})
    with sessions() as session:
        session.add(WorkerState(hostname="gone", state="online", last_seen_at=OLD))  # silent for an hour: not expected
        session.commit()
        assert reconcile._lost_candidates_held(make_config(app), session) == {"a", "b"}
        session.add(WorkerState(hostname="w2", state="online", last_seen_at=datetime.now(timezone.utc)))
        session.commit()
        assert reconcile._lost_candidates_held(make_config(app), session) is None  # w2 is expected but silent
