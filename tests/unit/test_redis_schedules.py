"""Optimistic conflict checks for RedBeat edits, enabled-state stamping in the
schedule sync, and the quiet refresh used by long-running processes."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery
from celery.schedules import crontab, schedule
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

pytest.importorskip("redbeat")
fakeredis = pytest.importorskip("fakeredis")

from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.external import ExternalUser
from celery_manager.db.models import Base, PeriodicSchedule
from celery_manager.schedulers import ScheduleEntry
from celery_manager.schedulers.base import ScheduleConflict
from celery_manager.schedulers.redbeat import RedBeatBackend
from celery_manager.schedulers.service import refresh_quietly, sync_from_backend

NAME = "nightly-report"
TASK = "shop.tasks.send_digest"


@pytest.fixture
def redbeat():
    app = Celery("redbeat-conflicts", broker="memory://")
    app.conf.timezone = "UTC"
    with patch("redbeat.schedulers.get_redis", return_value=fakeredis.FakeRedis()):
        backend = RedBeatBackend(app)
        backend.create(ScheduleEntry(NAME, TASK, schedule(60), source=backend.name))
        yield backend


@pytest.fixture
def sessions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _simulate_beat_firing(backend, name, when):
    stored = backend.Entry.from_key(backend.conf.key_prefix + name, app=backend.app)
    stored.last_run_at = when
    stored.save()


def test_redbeat_update_with_stale_expected_conflicts(redbeat):
    seen = redbeat.get(NAME)
    redbeat.update(NAME, expected=seen, enabled=False)  # someone else's edit lands first
    with pytest.raises(ScheduleConflict):
        redbeat.update(NAME, expected=seen, schedule=schedule(120))
    assert redbeat.get(NAME).enabled is False
    assert redbeat.get(NAME).schedule.run_every.total_seconds() == 60


def test_redbeat_update_with_fresh_expected_succeeds(redbeat):
    redbeat.update(NAME, expected=redbeat.get(NAME), enabled=False)
    fresh = redbeat.get(NAME)
    saved = redbeat.update(NAME, expected=fresh, schedule=schedule(120))
    assert saved.schedule.run_every.total_seconds() == 120
    assert redbeat.get(NAME).schedule.run_every.total_seconds() == 120


def test_redbeat_update_ignores_last_run_at_changes(redbeat):
    seen = redbeat.get(NAME)
    _simulate_beat_firing(redbeat, NAME, datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc))
    saved = redbeat.update(NAME, expected=seen, enabled=False)
    assert saved.enabled is False


def test_redbeat_delete_with_stale_expected_conflicts(redbeat):
    seen = redbeat.get(NAME)
    redbeat.update(NAME, expected=seen, enabled=False)
    with pytest.raises(ScheduleConflict):
        redbeat.delete(NAME, expected=seen)
    assert redbeat.get(NAME) is not None


def test_redbeat_delete_with_fresh_expected_and_last_run_change_succeeds(redbeat):
    seen = redbeat.get(NAME)
    _simulate_beat_firing(redbeat, NAME, datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc))
    redbeat.delete(NAME, expected=seen)
    assert redbeat.get(NAME) is None


def test_sync_stamps_enabled_changed_at_only_on_reenable(sessions):
    backend = MagicMock(name="fake-backend")
    backend.name = "fake"

    def entry(enabled):
        return ScheduleEntry(NAME, TASK, crontab(minute=0), enabled=enabled, source="fake")

    with sessions() as session:
        sync_from_backend(session, backend, [entry(enabled=True)])
        assert session.query(PeriodicSchedule).filter_by(name=NAME).one().enabled_changed_at is None

    with sessions() as session:
        sync_from_backend(session, backend, [entry(enabled=False)])
    with sessions() as session:
        before = datetime.now(timezone.utc)
        sync_from_backend(session, backend, [entry(enabled=True)])
        stamped = session.query(PeriodicSchedule).filter_by(name=NAME).one().enabled_changed_at
    assert stamped is not None and stamped.replace(tzinfo=timezone.utc) >= before - timedelta(seconds=1)

    with sessions() as session:
        sync_from_backend(session, backend, [entry(enabled=True)])
        assert session.query(PeriodicSchedule).filter_by(name=NAME).one().enabled_changed_at == stamped


def test_refresh_quietly_is_noop_without_backend(sessions):
    config = CeleryManagerConfig(celery_app=Celery("no-backend"), db_url="sqlite:///:memory:", secret_key="s")
    refresh_quietly(config, sessions)
    assert not hasattr(config, "_schedule_service")


def test_refresh_quietly_swallows_backend_errors(sessions):
    backend = MagicMock(name="broken-backend")
    backend.name = "broken"
    backend.list.side_effect = RuntimeError("redis is down")
    config = CeleryManagerConfig(celery_app=Celery("broken"), db_url="sqlite:///:memory:", secret_key="s",
                                 scheduler_backend=backend, schedule_sync_interval_seconds=0)
    refresh_quietly(config, sessions)  # must not raise
    assert config._schedule_service.entries == {}


class _StaleBackend:
    """Reads succeed, but every edit reports that the stored entry moved on."""
    name = "stale-test"
    writable = True
    checks_conflicts = True

    def __init__(self, entry):
        self.entries = {entry.name: entry}

    def list(self):
        return list(self.entries.values())

    def get(self, name):
        return self.entries.get(name)

    def update(self, name, expected=None, **changes):
        raise ScheduleConflict(name)

    def delete(self, name, expected=None):
        raise ScheduleConflict(name)


def test_patch_route_returns_409_on_conflict():
    backend = _StaleBackend(ScheduleEntry(NAME, TASK, schedule(60), source="stale-test"))
    app = Celery("patch-conflict", broker="memory://")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s",
                                 scheduler_backend=backend, schedule_sync_interval_seconds=0,
                                 authenticate_request=lambda r: ExternalUser("host", "admin", "person-1"))
    api, _ = create_app(config)
    client = TestClient(api, base_url="https://testserver")
    response = client.patch(f"/api/schedules/{NAME}", json={"enabled": False})
    assert response.status_code == 409
