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


# --- The listener half of the C1 merge: `skipped` is decorator-owned ---
#
# The decorator records status="skipped" when @track matches a schedule
# override and never calls the task body. Celery knows nothing about that —
# the wrapper returns normally, so the worker emits an ordinary
# task-succeeded for the same uuid. In the documented two-process topology
# that event arrives after the decorator's write and used to flip the row
# back to "succeeded", so a paused occurrence displayed as if it had run.


def test_listener_does_not_overwrite_a_decorator_written_skipped_status():
    session = _session()
    # Stand in for the decorator's write (status the listener has no event for).
    session.add(TaskRun(task_id="paused-1", task_name="demo.report", status="skipped"))
    session.commit()

    upsert_task_run(session, {"type": "task-started", "uuid": "paused-1", "hostname": "worker-1"})
    upsert_task_run(session, {"type": "task-succeeded", "uuid": "paused-1", "runtime": 0.01})
    session.commit()

    run = session.query(TaskRun).filter_by(task_id="paused-1").one()
    assert run.status == "skipped"
    # Only `status` is protected: the listener still contributes what it alone
    # knows, so a skipped run isn't left without worker/runtime detail.
    assert run.worker == "worker-1"
    assert run.runtime_seconds == 0.01


def test_no_lifecycle_event_can_overwrite_skipped():
    """task-failed / task-retried / task-revoked must be guarded too, not just
    the task-succeeded that motivated the fix."""
    for event_type in ("task-succeeded", "task-failed", "task-retried", "task-revoked"):
        session = _session()
        session.add(TaskRun(task_id="paused-x", task_name="demo.report", status="skipped"))
        session.commit()

        upsert_task_run(session, {"type": event_type, "uuid": "paused-x"})
        session.commit()

        run = session.query(TaskRun).filter_by(task_id="paused-x").one()
        assert run.status == "skipped", event_type


def test_normal_status_transitions_are_unaffected_by_the_skipped_guard():
    """The guard must be narrow: a task that was never skip-decorated still
    gets its status updated by every lifecycle event, including overwriting an
    earlier terminal status on retry."""
    session = _session()
    upsert_task_run(session, {"type": "task-sent", "uuid": "normal-1", "name": "demo.echo"})
    session.commit()
    assert session.query(TaskRun).filter_by(task_id="normal-1").one().status == "queued"

    upsert_task_run(session, {"type": "task-started", "uuid": "normal-1", "hostname": "w1"})
    session.commit()
    assert session.query(TaskRun).filter_by(task_id="normal-1").one().status == "started"

    upsert_task_run(session, {"type": "task-failed", "uuid": "normal-1", "exception": "ValueError()"})
    session.commit()
    assert session.query(TaskRun).filter_by(task_id="normal-1").one().status == "failed"

    # A previously-failed row is not decorator-owned, so a later event still wins.
    upsert_task_run(session, {"type": "task-retried", "uuid": "normal-1"})
    session.commit()
    assert session.query(TaskRun).filter_by(task_id="normal-1").one().status == "retried"


def test_decorator_and_listener_converge_on_skipped_end_to_end(monkeypatch):
    """The full two-process sequence, driven through the real decorator rather
    than a hand-written 'skipped' row."""
    from celery import Celery

    from celery_manager import decorator as decorator_module
    from celery_manager.db.models import ScheduleOverride
    from celery_manager.decorator import track

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    # monkeypatch (not configure()) so this test's session factory is restored
    # even if it fails partway — the decorator's wiring is module-global state.
    monkeypatch.setattr(decorator_module, "_session_factory", Session)
    monkeypatch.setattr(decorator_module, "_eta_tolerance_seconds", 60)

    app = Celery("guard_test", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    ran = []

    @app.task(name="demo.guarded_report", bind=True)
    @track(schedule_name="demo-every-2h")
    def report(self):
        ran.append(True)

    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h", target_time=datetime.now(timezone.utc),
            action="skip", consumed=False, created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    report.apply(task_id="e2e-paused-1")
    assert ran == []  # body never ran

    # The separate events-listener process now processes the worker's events.
    with Session() as session:
        upsert_task_run(session, {"type": "task-succeeded", "uuid": "e2e-paused-1", "runtime": 0.01})
        session.commit()

    with Session() as session:
        rows = session.query(TaskRun).filter_by(task_id="e2e-paused-1").all()
        assert len(rows) == 1
        assert rows[0].status == "skipped"
