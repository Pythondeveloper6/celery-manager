from datetime import datetime, timezone
from unittest.mock import MagicMock

from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun
from celery_manager.control.tasks import list_task_runs, get_task_run, start_task, cancel_task


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([
            TaskRun(task_id="t1", task_name="demo.echo", status="succeeded"),
            TaskRun(task_id="t2", task_name="demo.echo", status="failed"),
            TaskRun(task_id="t3", task_name="demo.other", status="succeeded"),
        ])
        session.commit()
    return Session()


def test_list_task_runs_filters_by_status():
    session = _session()
    results = list_task_runs(session, status="failed")
    assert [r.task_id for r in results] == ["t2"]


def test_list_task_runs_filters_by_name():
    session = _session()
    results = list_task_runs(session, task_name="demo.other")
    assert [r.task_id for r in results] == ["t3"]


def test_get_task_run_returns_none_when_missing():
    session = _session()
    assert get_task_run(session, "does-not-exist") is None


def test_start_task_calls_apply_async():
    app = MagicMock(spec=Celery)
    app.send_task.return_value = MagicMock(id="new-task-id")

    task_id = start_task(app, "demo.echo", args=["hi"], kwargs={})

    app.send_task.assert_called_once_with("demo.echo", args=["hi"], kwargs={})
    assert task_id == "new-task-id"


def test_cancel_task_revokes_by_default():
    app = MagicMock(spec=Celery)
    cancel_task(app, "t1")
    app.control.revoke.assert_called_once_with("t1", terminate=False)


def test_cancel_task_terminates_when_requested():
    app = MagicMock(spec=Celery)
    cancel_task(app, "t1", terminate=True)
    app.control.revoke.assert_called_once_with("t1", terminate=True)
