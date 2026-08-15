# tests/unit/test_decorator.py
from datetime import datetime, timedelta, timezone

import pytest
from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, ScheduleOverride, TaskRun
from celery_manager.decorator import track, configure


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
