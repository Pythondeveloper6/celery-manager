# tests/unit/test_decorator.py
from datetime import datetime, timedelta, timezone

import pytest
from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager import decorator as decorator_module
from celery_manager.db.models import Base, ScheduleOverride, TaskRun
from celery_manager.decorator import track, configure
from celery_manager.events.listener import upsert_task_run


@pytest.fixture
def env():
    app = Celery("test_app", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    configure(session_factory=Session, eta_tolerance_seconds=60)

    calls = []

    @app.task(name="demo.echo", bind=True)
    @track(schedule_name="demo-every-2h")
    def echo(self, value):
        calls.append(value)
        return value

    return app, Session, echo, calls


def test_runs_normally_when_no_override(env):
    app, Session, echo, calls = env
    echo.apply(args=["hi"])
    assert calls == ["hi"]


def test_skips_when_override_matches_now(env):
    app, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime.now(timezone.utc),
            action="skip",
            consumed=False,
            created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    echo.apply(args=["hi"])
    assert calls == []  # task body did not run

    with Session() as session:
        run = session.query(TaskRun).filter_by(task_name="demo.echo").one()
        assert run.status == "skipped"

        override = session.query(ScheduleOverride).filter_by(schedule_name="demo-every-2h").one()
        assert override.consumed is True


def test_ignores_override_outside_tolerance_window(env):
    app, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime.now(timezone.utc) - timedelta(hours=1),
            action="skip",
            consumed=False,
            created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    echo.apply(args=["hi"])
    assert calls == ["hi"]  # override is stale, task ran


# --- C1 regressions: decorator/events-listener collision on task_runs.task_id ---
#
# `task_runs.task_id` is UNIQUE, and in the documented deployment topology the
# events listener runs as its own process next to the worker — so it can insert
# a row for a task's UUID (from task-sent/task-started) *before* the decorated
# body finishes and the decorator writes its own outcome. The decorator used to
# blindly session.add(TaskRun(...)), which raised IntegrityError and made a
# task whose body SUCCEEDED report as FAILURE. Reproduced directly below.


def test_listener_row_then_decorated_run_converges_to_one_row(env):
    """(a) A listener-inserted row followed by that same task_id's decorated
    run must converge to ONE row carrying the decorator's final outcome —
    not an IntegrityError, and not a second row."""
    app, Session, echo, calls = env
    task_id = "shared-uuid-1"

    # The events listener process gets there first.
    with Session() as session:
        upsert_task_run(session, {"type": "task-sent", "uuid": task_id, "name": "demo.echo", "queue": "default"})
        upsert_task_run(session, {"type": "task-started", "uuid": task_id, "hostname": "worker-1"})
        session.commit()

    result = echo.apply(args=["hi"], task_id=task_id)

    # The task itself must be unaffected by who else wrote to storage.
    assert result.state == "SUCCESS"
    assert result.result == "hi"
    assert calls == ["hi"]

    with Session() as session:
        rows = session.query(TaskRun).filter_by(task_id=task_id).all()
        assert len(rows) == 1
        row = rows[0]
        # Decorator owns the final status transition and log capture...
        assert row.status == "succeeded"
        assert row.log_output is not None
        # ...but merges rather than overwrites: the listener owns worker/queue,
        # and a decorator write must never null those back out.
        assert row.worker == "worker-1"
        assert row.queue == "default"


def test_listener_row_then_failing_decorated_run_records_failure_on_same_row(env):
    """The failure branch upserts too — and the task's real exception still
    propagates rather than being displaced by a storage error."""
    app, Session, echo, calls = env
    task_id = "shared-uuid-2"

    @app.task(name="demo.boom", bind=True)
    @track()
    def boom(self):
        raise ValueError("kaboom")

    with Session() as session:
        upsert_task_run(session, {"type": "task-sent", "uuid": task_id, "name": "demo.boom", "queue": "default"})
        session.commit()

    result = boom.apply(task_id=task_id)

    assert result.state == "FAILURE"
    assert isinstance(result.result, ValueError)
    assert str(result.result) == "kaboom"

    with Session() as session:
        rows = session.query(TaskRun).filter_by(task_id=task_id).all()
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert "kaboom" in rows[0].exception
        assert "ValueError" in rows[0].traceback


def test_skip_path_consumes_override_even_if_task_run_write_fails(env, monkeypatch):
    """(b) The skip decision must genuinely survive a downstream storage
    error. `consumed = True` is committed on its own, before the task_runs
    write — otherwise a failed write would roll it back and the same override
    would silently skip a *later*, unrelated run of this schedule."""
    app, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime.now(timezone.utc),
            action="skip",
            consumed=False,
            created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    def exploding_upsert(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(decorator_module, "_upsert_task_run", exploding_upsert)

    result = echo.apply(args=["hi"], task_id="skip-uuid-1")

    # Body still skipped, and the storage failure did not surface as a task failure.
    assert calls == []
    assert result.state == "SUCCESS"

    with Session() as session:
        override = session.query(ScheduleOverride).filter_by(schedule_name="demo-every-2h").one()
        assert override.consumed is True


def test_persistence_failure_never_turns_a_successful_task_into_a_failure(env, monkeypatch):
    """Spec §4.1: celery-manager must never be the reason a task's reported
    outcome changes."""
    app, Session, echo, calls = env

    def exploding_upsert(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(decorator_module, "_upsert_task_run", exploding_upsert)

    result = echo.apply(args=["hi"], task_id="ok-uuid-1")

    assert calls == ["hi"]
    assert result.state == "SUCCESS"
    assert result.result == "hi"


def test_persistence_failure_still_propagates_a_real_task_exception(env, monkeypatch):
    """The mirror of the above: swallowing storage errors must not swallow
    the task's own exception."""
    app, Session, echo, calls = env

    @app.task(name="demo.boom2", bind=True)
    @track()
    def boom(self):
        raise ValueError("real failure")

    def exploding_upsert(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(decorator_module, "_upsert_task_run", exploding_upsert)

    result = boom.apply(task_id="boom-uuid-1")

    assert result.state == "FAILURE"
    assert isinstance(result.result, ValueError)
    assert str(result.result) == "real failure"


def test_unconfigured_decorator_still_runs_the_task_body(env, monkeypatch):
    """The common single-process/eager-testing case (no listener, never
    configured) must keep working exactly as before: pass-through, no raise."""
    app, Session, echo, calls = env
    monkeypatch.setattr(decorator_module, "_session_factory", None)

    result = echo.apply(args=["hi"])

    assert calls == ["hi"]
    assert result.state == "SUCCESS"
    with Session() as session:
        assert session.query(TaskRun).count() == 0
