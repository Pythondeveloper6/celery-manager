"""Optional scheduler storage smoke gate on CI's disposable SQL servers."""
import os
from uuid import uuid4

import pytest
from celery import Celery
from celery.schedules import schedule
from sqlalchemy import create_engine, inspect
from sqlalchemy.schema import CreateSchema, DropSchema

pytest.importorskip("sqlalchemy_celery_beat")

from celery_manager.schedulers import ScheduleEntry, SQLAlchemyBeatBackend

pytestmark = pytest.mark.skipif(not os.environ.get("CM_STORAGE_DB_URL"), reason="needs disposable SQL database")


def test_sql_schedule_server_roundtrip_and_restart():
    url = os.environ["CM_STORAGE_DB_URL"]
    schema = os.environ.get("CM_STORAGE_SCHEMA") or None
    raw = create_engine(url)
    backend = None
    created_schema = False
    try:
        if schema:
            with raw.begin() as connection:
                if not inspect(connection).has_schema(schema):
                    connection.execute(CreateSchema(schema))
                    created_schema = True
        app = Celery("sql-server-contract", broker="memory://")
        app.conf.update(beat_dburi=url, beat_schema=schema, timezone="UTC")
        backend = SQLAlchemyBeatBackend(app)
        backend.initialize()
        name = f"cm-contract-{uuid4().hex}"
        backend.create(ScheduleEntry(name, "contract.task", schedule(60), options={"queue": "slow"}))
        backend.update(name, enabled=False, schedule=schedule(120))
        reopened = SQLAlchemyBeatBackend(app)
        try:
            saved = reopened.get(name)
            assert saved.enabled is False
            assert saved.options["queue"] == "slow"
            assert saved.schedule.run_every.total_seconds() == 120
        finally:
            reopened.close()
        backend.delete(name)
        assert backend.get(name) is None
    finally:
        try:
            if backend is not None:
                backend.metadata.drop_all(backend.engine)
            if created_schema:
                with raw.begin() as connection:
                    connection.execute(DropSchema(schema))
        finally:
            if backend is not None:
                backend.close()
            raw.dispose()
