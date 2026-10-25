from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, PeriodicSchedule, ScheduleOverride, TaskRun


def test_task_run_round_trip():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        run = TaskRun(
            task_id="abc-123",
            task_name="demo.send_report",
            status="succeeded",
            queue="default",
            args="[]",
            kwargs="{}",
            eta=None,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        session.add(run)
        session.commit()

        fetched = session.query(TaskRun).filter_by(task_id="abc-123").one()
        assert fetched.task_name == "demo.send_report"
        assert fetched.status == "succeeded"


def test_schedule_override_round_trip():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        sched = PeriodicSchedule(
            name="demo-every-2h",
            task_name="demo.send_report",
            schedule_spec="2h",
            enabled=True,
        )
        session.add(sched)
        session.commit()

        override = ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc),
            action="skip",
            created_by="alice",
        )
        session.add(override)
        session.commit()

        fetched = (
            session.query(ScheduleOverride)
            .filter_by(schedule_name="demo-every-2h")
            .one()
        )
        assert fetched.action == "skip"
        assert fetched.consumed is False
