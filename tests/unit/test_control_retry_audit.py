from unittest.mock import MagicMock

from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun, AuditLog
from celery_manager.control.audit import record
from celery_manager.control.tasks import retry_task, bulk_retry


def _session_with_runs():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([
            TaskRun(task_id="t1", task_name="demo.echo", status="failed", args="[1]", kwargs="{}"),
            TaskRun(task_id="t2", task_name="demo.echo", status="failed", args="[2]", kwargs="{}"),
        ])
        session.commit()
    return Session()


def test_record_writes_audit_row():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    record(session, "alice", "cancel_occurrence", "schedule", "demo-every-2h", {"target_time": "2026-08-15T20:00:00Z"})

    row = session.query(AuditLog).one()
    assert row.username == "alice"
    assert row.action == "cancel_occurrence"


def test_retry_task_resends_original_args_and_logs_audit():
    session = _session_with_runs()
    app = MagicMock(spec=Celery)
    app.send_task.return_value = MagicMock(id="t1-retry")

    new_id = retry_task(app, session, "t1", username="alice")

    app.send_task.assert_called_once_with("demo.echo", args=[1], kwargs={})
    assert new_id == "t1-retry"
    audit_rows = session.query(AuditLog).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "retry"


def test_bulk_retry_returns_all_new_ids():
    session = _session_with_runs()
    app = MagicMock(spec=Celery)
    app.send_task.side_effect = [MagicMock(id="t1-retry"), MagicMock(id="t2-retry")]

    new_ids = bulk_retry(app, session, ["t1", "t2"], username="alice")

    assert new_ids == ["t1-retry", "t2-retry"]
    assert session.query(AuditLog).count() == 2
