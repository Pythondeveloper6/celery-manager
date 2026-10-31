"""Shared release upgrade contract for SQLite and disposable SQL servers."""
from datetime import datetime, timedelta, timezone

from alembic import command
from sqlalchemy import MetaData, Table, inspect, select, update

from celery_manager.db.auth_state import BrowserSession, LoginBackoff
from celery_manager.db.failures import FailureResolution, FailureResolutionHistory
from celery_manager.db.health_data import (
    ExpectedSchedulerCommand,
    HealthObservation,
    SchedulerInstance,
)
from celery_manager.db.migrate import alembic_config, migration_status
from celery_manager.db.models import (
    AppSetting,
    AuditLog,
    PeriodicSchedule,
    TaskRun,
    User,
    WorkerState,
)
from celery_manager.db.operations import OperationAnnotation
from celery_manager.db.worker_history import WorkerHeartbeatSample

RELEASE_TABLES = {"failure_resolutions", "failure_resolution_history", "browser_sessions", "login_backoff",
                  "worker_heartbeat_samples", "health_observations", "scheduler_instances", "expected_scheduler_commands", "operation_annotations"}


def exercise_release_upgrade(engine, schema=None):
    at = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0014")
        legacy = [
            (TaskRun, {"task_id": "release-old-run", "task_name": "project.task", "status": "succeeded",
                       "args": '["keep me"]', "kwargs": "{}", "retry_count": 0, "log_output_truncated": False, "result_truncated": False, "result_repr": '{"value":42}', "runtime_seconds": 1.25}),
            (PeriodicSchedule, {"name": "release-old-schedule", "task_name": "project.task", "schedule_spec": "1d",
                                "args": "[]", "kwargs": "{}", "enabled": False, "paused_at": at, "paused_by": "operator"}),
            (User, {"username": "release-operator", "password_hash": "preserve-password-hash", "role": "operator"}),
            (AppSetting, {"key": "release-setting", "value": '{"private":"preserve"}', "updated_at": at}),
            (AuditLog, {"username": "release-operator", "action": "legacy-action", "target_type": "task",
                        "target_id": "release-old-run", "details": '{"before":true}', "created_at": at}),
            (WorkerState, {"hostname": "release-worker", "state": "online", "last_seen_at": at,
                           "active": 2, "processed": 42}),
        ]
        legacy_tables = {model.__tablename__: Table(model.__tablename__, MetaData(), schema=schema, autoload_with=connection) for model, _ in legacy}
        for model, values in legacy:
            connection.execute(legacy_tables[model.__tablename__].insert().values(**values))
        before = {model.__tablename__: connection.execute(select(legacy_tables[model.__tablename__])).mappings().all()
                  for model, _ in legacy}
        command.upgrade(alembic_config(connection), "0020")
    assert migration_status(engine).current == "0020"
    assert RELEASE_TABLES <= set(inspect(engine).get_table_names(schema=schema))
    with engine.begin() as connection:
        migrated = connection.execute(select(TaskRun.__table__).where(TaskRun.task_id == "release-old-run")).mappings().one()
        assert migrated["source"] == "celery" and migrated["tenant_id"] is None
        metadata = {"source": "external", "tenant_id": "release-tenant", "context_json": '{"work":"preserve"}',
                    "operation_key": "release-operation", "correlation_id": "release-correlation"}
        connection.execute(update(TaskRun.__table__).where(TaskRun.task_id == "release-old-run").values(**metadata, updated_at=migrated["updated_at"]))
        new = [
            (FailureResolution, {"task_id": "release-old-run", "state": "acknowledged", "actor": "operator", "updated_at": at}),
            (FailureResolutionHistory, {"task_id": "release-old-run", "state": "acknowledged", "actor": "operator", "created_at": at}),
            (BrowserSession, {"session_id": "release-session", "username": "release-operator", "source": "local",
                              "created_at": at, "expires_at": at + timedelta(days=1)}),
            (LoginBackoff, {"key": "release-backoff", "attempts": 2, "updated_at": at}),
            (WorkerHeartbeatSample, {"hostname": "release-worker", "bucket_at": at, "observed_at": at,
                                     "state": "online", "active": 2, "processed": 42}),
            (HealthObservation, {"name": "release-probe", "state": "healthy", "metrics": '{"depth":2}', "observed_at": at}),
            (SchedulerInstance, {"instance_id": "release-scheduler", "registered_at": at, "heartbeat_at": at}),
            (ExpectedSchedulerCommand, {"instance_id": "release-scheduler", "name": "release-command",
                                        "schedule_spec": '{"type":"crontab","minute":"0","hour":"9","day_of_month":"*","month_of_year":"*","day_of_week":"*","timezone":"UTC"}',
                                        "grace_seconds": 60, "registered_at": at, "observed_at": at}),
            (OperationAnnotation, {"kind": "deployment", "environment": "production", "label": "Release test",
                                   "revision": "0014-to-0020", "note": "Preserve annotation", "starts_at": at,
                                   "created_at": at, "actor": "release-operator"}),
        ]
        for model, values in new:
            connection.execute(model.__table__.insert().values(**values))
        new_before = {model.__tablename__: connection.execute(select(model.__table__)).mappings().all()
                      for model, _ in new}
        # Repeated deployment is a no-op for both old and new data.
        command.upgrade(alembic_config(connection), "0020")
        for model, _ in legacy:
            assert connection.execute(select(legacy_tables[model.__tablename__])).mappings().all() == before[model.__tablename__]
        for model, _ in new:
            assert connection.execute(select(model.__table__)).mappings().all() == new_before[model.__tablename__]
        kept = connection.execute(select(TaskRun.__table__).where(TaskRun.task_id == "release-old-run")).mappings().one()
        assert all(kept[key] == value for key, value in metadata.items())
        # A deliberate rollback removes new feature tables and observation columns.
        # It retains every pre-release run, schedule, credential and audit value.
        command.downgrade(alembic_config(connection), "0014")
        for model, _ in legacy:
            assert connection.execute(select(legacy_tables[model.__tablename__])).mappings().all() == before[model.__tablename__]
    assert migration_status(engine).current == "0014"
    assert RELEASE_TABLES.isdisjoint(inspect(engine).get_table_names(schema=schema))
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0020")
        for model, _ in legacy:
            assert connection.execute(select(legacy_tables[model.__tablename__])).mappings().all() == before[model.__tablename__]
        for model, _ in new:
            assert connection.execute(select(model.__table__)).first() is None
        reset = connection.execute(select(TaskRun.__table__).where(TaskRun.task_id == "release-old-run")).mappings().one()
        assert reset["source"] == "celery"
        assert all(reset[key] is None for key in metadata if key != "source")
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
