from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun
from celery_manager.events.listener import upsert_task_run


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _event(kind, ago=0.0, **extra):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=ago)
    return {"type": kind, "uuid": "t1", "name": "demo.echo", "timestamp": stamp.timestamp(), **extra}


def test_started_uses_the_event_time_so_listener_delay_does_not_add_queue_wait():
    session = _session()
    upsert_task_run(session, _event("task-sent", ago=100))
    upsert_task_run(session, _event("task-started", ago=90))  # processed 90 s late
    run = session.query(TaskRun).one()
    delay = datetime.now(timezone.utc) - _utc(run.started_at)
    assert 89 < delay.total_seconds() < 92
    assert 9 < (_utc(run.started_at) - _utc(run.sent_at)).total_seconds() < 11


def test_finish_is_start_plus_runtime():
    session = _session()
    upsert_task_run(session, _event("task-started", ago=60))
    upsert_task_run(session, _event("task-succeeded", ago=1, runtime=12.5))
    run = session.query(TaskRun).one()
    assert (_utc(run.finished_at) - _utc(run.started_at)).total_seconds() == 12.5


def test_failed_without_runtime_uses_event_time():
    session = _session()
    upsert_task_run(session, _event("task-failed", ago=30))
    run = session.query(TaskRun).one()
    assert 29 < (datetime.now(timezone.utc) - _utc(run.finished_at)).total_seconds() < 32


def test_worker_clock_skew_falls_back_to_arrival_time_and_is_counted():
    session = _session()
    stats = SimpleNamespace(clock_skew=0)
    config = SimpleNamespace(event_clock_skew_seconds=300, store_results=True)
    upsert_task_run(session, _event("task-started", ago=3600), config, stats)
    run = session.query(TaskRun).one()
    assert (datetime.now(timezone.utc) - _utc(run.started_at)).total_seconds() < 5
    assert stats.clock_skew == 1


def test_event_without_timestamp_uses_arrival_time():
    session = _session()
    upsert_task_run(session, {"type": "task-started", "uuid": "t1", "name": "demo.echo"})
    run = session.query(TaskRun).one()
    assert (datetime.now(timezone.utc) - _utc(run.started_at)).total_seconds() < 5
