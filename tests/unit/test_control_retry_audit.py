from unittest.mock import MagicMock

from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun, AuditLog
from celery_manager.control.audit import record
from celery_manager.control.tasks import retry_task, bulk_retry, get_task_run, list_retries_of


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


# --- Task 19: retry lineage. `retry_task` stamps the run it dispatches with
# retried_from_id so the detail view can show the chain immediately, without
# waiting on (or requiring) the events listener. ---


def test_retry_task_stamps_retried_from_id_when_no_row_exists_yet():
    """The normal case: send_task() has just returned a brand-new uuid and
    nothing has written a task_runs row for it, so retry_task must create
    one."""
    session = _session_with_runs()
    app = MagicMock(spec=Celery)
    app.send_task.return_value = MagicMock(id="t1-retry")

    new_id = retry_task(app, session, "t1", username="alice")

    new_run = get_task_run(session, new_id)
    assert new_run is not None
    assert new_run.retried_from_id == "t1"
    assert new_run.task_name == "demo.echo"
    assert new_run.status == "queued"
    assert [r.task_id for r in list_retries_of(session, "t1")] == [new_id]


def test_retry_task_stamps_retried_from_id_when_the_row_already_exists():
    """The race the upsert exists for: the events listener runs as its own
    process and may already have inserted a row for this uuid from
    `task-sent` by the time we look. task_runs.task_id is UNIQUE, so a blind
    insert raises IntegrityError here (finding C1 of the final review) — and
    the listener-owned fields it wrote must survive."""
    session = _session_with_runs()
    session.add(TaskRun(
        task_id="t1-retry", task_name="demo.echo", status="started",
        args="[]", kwargs="{}", queue="reports", worker="worker-1@host",
    ))
    session.commit()

    app = MagicMock(spec=Celery)
    app.send_task.return_value = MagicMock(id="t1-retry")

    new_id = retry_task(app, session, "t1", username="alice")

    assert session.query(TaskRun).filter_by(task_id="t1-retry").count() == 1
    new_run = get_task_run(session, new_id)
    assert new_run.retried_from_id == "t1"
    assert new_run.queue == "reports"
    assert new_run.worker == "worker-1@host"
    assert new_run.status == "started"


def test_retry_task_records_the_dispatched_args_on_the_new_run():
    """An override passed to retry_task is what actually ran, so it is what
    the new run's row must show — not the original's args."""
    session = _session_with_runs()
    app = MagicMock(spec=Celery)
    app.send_task.return_value = MagicMock(id="t1-retry")

    new_id = retry_task(app, session, "t1", username="alice", args=[99], kwargs={"force": True})

    new_run = get_task_run(session, new_id)
    assert new_run.args == "[99]"
    assert new_run.kwargs == '{"force": true}'


def test_retrying_the_same_run_twice_records_both_children():
    session = _session_with_runs()
    app = MagicMock(spec=Celery)
    app.send_task.side_effect = [MagicMock(id="t1-retry-a"), MagicMock(id="t1-retry-b")]

    retry_task(app, session, "t1", username="alice")
    retry_task(app, session, "t1", username="alice")

    assert [r.task_id for r in list_retries_of(session, "t1")] == ["t1-retry-a", "t1-retry-b"]


def test_retry_task_lineage_does_not_disturb_the_audit_trail():
    """The lineage write commits before audit.record; the retry must still
    produce exactly one audit row, and only a `retry` one."""
    session = _session_with_runs()
    app = MagicMock(spec=Celery)
    app.send_task.return_value = MagicMock(id="t1-retry")

    retry_task(app, session, "t1", username="alice")

    rows = session.query(AuditLog).all()
    assert [r.action for r in rows] == ["retry"]
    assert rows[0].target_id == "t1"
    assert "t1-retry" in rows[0].details
