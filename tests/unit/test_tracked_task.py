from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.config import CeleryManagerConfig
from celery_manager.control.schedules import set_backend_pause
from celery_manager.db.models import Base, TaskRun
from celery_manager.decorator import configure, track
from celery_manager.doctor import doctor


def test_task_base_tracks_undecorated_tasks_once_and_respects_exclusions():
    app = Celery("tracked-base", broker="memory://", backend="cache+memory://",
                 task_cls="celery_manager.task:TrackedTask")
    app.conf.task_always_eager = True
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:",
                                 track_exclude=("demo.skip*",))
    configure(sessions, config=config)

    @app.task(name="demo.plain")
    def plain(value):
        return value.upper()

    @app.task(name="demo.decorated", bind=True)
    @track()
    def decorated(self, value):
        return value.upper()

    @app.task(name="demo.skipped")
    def skipped(value):
        return value.upper()

    assert plain.apply(args=["a"], task_id="plain-1").result == "A"
    assert decorated.apply(args=["b"], task_id="decorated-1").result == "B"
    assert skipped.apply(args=["c"], task_id="skipped-1").result == "C"
    with sessions() as session:
        assert [(r.task_id, r.status) for r in session.query(TaskRun).order_by(TaskRun.task_id)] == [
            ("decorated-1", "succeeded"), ("plain-1", "succeeded")
        ]


def test_doctor_names_scheduled_tasks_without_tracking(tmp_path):
    app = Celery("doctor-tracking", broker="memory://")

    @app.task(name="demo.plain")
    def plain():
        return None

    app.conf.beat_schedule = {"plain": {"task": plain.name, "schedule": 60}}
    config = CeleryManagerConfig(celery_app=app, db_url=f"sqlite:///{tmp_path / 'doctor.db'}")
    from celery_manager.db.migrate import upgrade_database
    from celery_manager.db.session import create_db_engine
    engine = create_db_engine(config.db_url)
    upgrade_database(engine)
    engine.dispose()
    checks = doctor(config)["checks"]
    assert any(c["name"] == "untracked_scheduled_tasks" and "demo.plain" in c["detail"] for c in checks)


def test_backend_pause_uses_resolved_writable_backend():
    from types import SimpleNamespace

    class Backend:
        writable = True
        enabled = True

        def get(self, name):
            return name == "hourly"

        def update(self, name, **changes):
            self.enabled = changes["enabled"]

    backend = Backend()
    config = SimpleNamespace(pause_mode="backend", scheduler_backend=None,
                             _schedule_service=SimpleNamespace(backend=backend))
    set_backend_pause(config, "hourly", True)
    assert backend.enabled is False


def test_beat_stamped_run_is_filed_under_the_entry_and_skipped_while_paused():
    from datetime import datetime, timezone

    from celery_manager.control.schedules import pause_schedule, resume_schedule
    from celery_manager.db.models import PeriodicSchedule
    from celery_manager.occurrences import OCCURRENCE_HEADER, SCHEDULE_HEADER

    app = Celery("tracked-pause", broker="memory://", backend="cache+memory://",
                 task_cls="celery_manager.task:TrackedTask")
    app.conf.task_always_eager = True
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    configure(sessions, config=CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:"))
    ran = []

    @app.task(name="shop.tasks.digest")
    def digest():
        ran.append(1)

    with sessions() as session:
        session.add(PeriodicSchedule(name="nightly-digest", task_name="shop.tasks.digest", schedule_spec="1d"))
        session.commit()
        pause_schedule(session, "nightly-digest", "operator")
    occurrence = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    headers = {SCHEDULE_HEADER: "nightly-digest", OCCURRENCE_HEADER: occurrence}
    digest.apply(task_id="paused-1", headers=headers)
    with sessions() as session:
        resume_schedule(session, "nightly-digest", "operator")
    digest.apply(task_id="resumed-1", headers=headers)

    assert ran == [1]
    with sessions() as session:
        rows = {r.task_id: (r.status, r.schedule_name) for r in session.query(TaskRun)}
    assert rows == {"paused-1": ("skipped", "nightly-digest"), "resumed-1": ("succeeded", "nightly-digest")}
