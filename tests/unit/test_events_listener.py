# tests/unit/test_events_listener.py
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun
from celery_manager.events.listener import upsert_task_run


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_task_sent_creates_row():
    session = _session()
    upsert_task_run(session, {
        "type": "task-sent",
        "uuid": "task-1",
        "name": "demo.echo",
        "queue": "default",
        "args": "['hi']",
        "kwargs": "{}",
    })
    session.commit()

    run = session.query(TaskRun).filter_by(task_id="task-1").one()
    assert run.task_name == "demo.echo"
    assert run.status == "queued"


def test_task_started_then_succeeded_updates_same_row():
    session = _session()
    upsert_task_run(session, {"type": "task-sent", "uuid": "task-2", "name": "demo.echo"})
    upsert_task_run(session, {"type": "task-started", "uuid": "task-2", "hostname": "worker-1"})
    upsert_task_run(session, {"type": "task-succeeded", "uuid": "task-2", "result": "'hi'", "runtime": 0.02})
    session.commit()

    rows = session.query(TaskRun).filter_by(task_id="task-2").all()
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].worker == "worker-1"
    assert rows[0].runtime_seconds == 0.02


def test_task_failed_records_exception():
    session = _session()
    upsert_task_run(session, {"type": "task-sent", "uuid": "task-3", "name": "demo.echo"})
    upsert_task_run(session, {
        "type": "task-failed",
        "uuid": "task-3",
        "exception": "ValueError('boom')",
        "traceback": "Traceback ...",
    })
    session.commit()

    run = session.query(TaskRun).filter_by(task_id="task-3").one()
    assert run.status == "failed"
    assert "boom" in run.exception
