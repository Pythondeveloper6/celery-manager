from datetime import datetime, timezone

import pytest
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.control.pause_import import import_pauses
from celery_manager.db.models import AuditLog, PeriodicSchedule
from celery_manager.db.session import get_session_factory, init_db


@pytest.fixture
def sessions():
    engine = init_db(CeleryManagerConfig(celery_app=Celery("import"), db_url="sqlite:///:memory:"))
    factory = get_session_factory(engine)
    with factory() as session:
        session.add_all([PeriodicSchedule(name=name, task_name="demo", schedule_spec="60") for name in ("one", "two")])
        session.commit()
    yield factory
    engine.dispose()


def test_import_is_dry_by_default_and_commits_all_with_audit(sessions):
    text = "schedule_name,paused_until\none,\ntwo,2099-01-01T12:00:00+02:00\n"
    with sessions() as session:
        result = import_pauses(session, text, "admin")
        assert result["count"] == 2
        assert all(r.paused_at is None for r in session.query(PeriodicSchedule))
        assert session.query(AuditLog).count() == 0
        import_pauses(session, text, "admin", dry_run=False)
    with sessions() as session:
        assert all(r.paused_by == "admin" for r in session.query(PeriodicSchedule))
        assert session.query(PeriodicSchedule).filter_by(name="two").one().paused_until.hour == 10
        assert session.query(AuditLog).one().action == "pause_import"


@pytest.mark.parametrize("tail", ["missing,", "one,", "two,2020-01-01T00:00:00Z", "two,2099-01-01T00:00:00", "two,bad", "two,,extra"])
def test_invalid_later_row_never_partially_applies(sessions, tail):
    with sessions() as session:
        with pytest.raises(ValueError):
            import_pauses(session, f"schedule_name,paused_until\none,\n{tail}\n", "admin", dry_run=False,
                          now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert all(r.paused_at is None for r in session.query(PeriodicSchedule))
        assert session.query(AuditLog).count() == 0
