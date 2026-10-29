"""Shared release upgrade contract for SQLite and disposable SQL servers."""
from datetime import datetime, timedelta, timezone

from alembic import command
from sqlalchemy import inspect, select

from celery_manager.db.auth_state import BrowserSession, LoginBackoff
from celery_manager.db.failures import FailureResolution, FailureResolutionHistory
from celery_manager.db.migrate import alembic_config, migration_status
from celery_manager.db.models import (
    AppSetting,
    AuditLog,
    PeriodicSchedule,
    TaskRun,
    User,
    WorkerState,
)
from celery_manager.db.worker_history import WorkerHeartbeatSample

RELEASE_TABLES = {"failure_resolutions", "failure_resolution_history", "browser_sessions", "login_backoff",
                  "worker_heartbeat_samples"}


def exercise_release_upgrade(engine, schema=None):
    at = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0014")
        legacy = [
            (TaskRun, {"task_id": "release-old-run", "task_name": "project.task", "status": "succeeded",
                       "args": '["keep me"]', "result_repr": '{"value":42}', "runtime_seconds": 1.25}),
            (PeriodicSchedule, {"name": "release-old-schedule", "task_name": "project.task", "schedule_spec": "1d",
                                "args": "[]", "kwargs": "{}", "enabled": False, "paused_at": at, "paused_by": "operator"}),
            (User, {"username": "release-operator", "password_hash": "preserve-password-hash", "role": "operator"}),
            (AppSetting, {"key": "release-setting", "value": '{"private":"preserve"}', "updated_at": at}),
            (AuditLog, {"username": "release-operator", "action": "legacy-action", "target_type": "task",
                        "target_id": "release-old-run", "details": '{"before":true}', "created_at": at}),
            (WorkerState, {"hostname": "release-worker", "state": "online", "last_seen_at": at,
                           "active": 2, "processed": 42}),
        ]
        for model, values in legacy:
            connection.execute(model.__table__.insert().values(**values))
        before = {model.__tablename__: connection.execute(select(model.__table__)).mappings().all()
                  for model, _ in legacy}
        command.upgrade(alembic_config(connection), "0017")
    assert migration_status(engine).current == "0017"
    assert RELEASE_TABLES <= set(inspect(engine).get_table_names(schema=schema))
    with engine.begin() as connection:
        new = [
            (FailureResolution, {"task_id": "release-old-run", "state": "acknowledged", "actor": "operator", "updated_at": at}),
            (FailureResolutionHistory, {"task_id": "release-old-run", "state": "acknowledged", "actor": "operator", "created_at": at}),
            (BrowserSession, {"session_id": "release-session", "username": "release-operator", "source": "local",
                              "created_at": at, "expires_at": at + timedelta(days=1)}),
            (LoginBackoff, {"key": "release-backoff", "attempts": 2, "updated_at": at}),
            (WorkerHeartbeatSample, {"hostname": "release-worker", "bucket_at": at, "observed_at": at,
                                     "state": "online", "active": 2, "processed": 42}),
        ]
        for model, values in new:
            connection.execute(model.__table__.insert().values(**values))
        new_before = {model.__tablename__: connection.execute(select(model.__table__)).mappings().all()
                      for model, _ in new}
        # Repeated deployment is a no-op for both old and new data.
        command.upgrade(alembic_config(connection), "0017")
        for model, _ in legacy:
            assert connection.execute(select(model.__table__)).mappings().all() == before[model.__tablename__]
        for model, _ in new:
            assert connection.execute(select(model.__table__)).mappings().all() == new_before[model.__tablename__]
        # A deliberate rollback removes only the new feature tables and data.
        command.downgrade(alembic_config(connection), "0014")
        for model, _ in legacy:
            assert connection.execute(select(model.__table__)).mappings().all() == before[model.__tablename__]
    assert migration_status(engine).current == "0014"
    assert RELEASE_TABLES.isdisjoint(inspect(engine).get_table_names(schema=schema))
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0017")
        for model, _ in legacy:
            assert connection.execute(select(model.__table__)).mappings().all() == before[model.__tablename__]
        for model, _ in new:
            assert connection.execute(select(model.__table__)).first() is None
    # SQLite does not enforce VARCHAR lengths; run this on real servers too.
    from types import SimpleNamespace

    from celery import Celery
    from sqlalchemy.orm import sessionmaker

    from celery_manager.beat_health import owner_key, record_tick
    app = Celery("release-heartbeat", broker="memory://")
    app.conf.update(celery_manager_db_url="configured", cm_beat_owner_id="release-owner")
    sessions = sessionmaker(bind=engine)
    record_tick(SimpleNamespace(app=app, _cm_health_sessions=sessions))
    with sessions() as session:
        assert session.get(AppSetting, owner_key("release-owner")) is not None
        assert session.get(AppSetting, "beat_last_tick") is not None
