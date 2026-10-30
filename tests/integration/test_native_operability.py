"""Strict opt-in gate on disposable PostgreSQL/MySQL/SQL Server instances."""
import os

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect
from sqlalchemy.schema import CreateSchema, DropSchema

from celery_manager import CeleryManagerConfig
from celery_manager.db.migrate import VERSION_TABLE
from celery_manager.db.models import Base
from celery_manager.db.session import create_db_engine
from tests.native_operability import exercise_native_operability

pytestmark = pytest.mark.skipif(not os.environ.get("CM_STORAGE_DB_URL"), reason="needs disposable SQL database")


def test_native_observation_health_and_scheduler_server_contract():
    url = os.environ["CM_STORAGE_DB_URL"]
    namespace = os.environ.get("CM_STORAGE_SCHEMA")
    schema = namespace + "_native" if namespace else None
    raw, engine, created = create_engine(url), None, False
    try:
        if schema:
            with raw.begin() as connection:
                if not inspect(connection).has_schema(schema):
                    connection.execute(CreateSchema(schema)); created = True
        config = CeleryManagerConfig(celery_app=None, db_url=url, db_schema=schema)
        exercise_native_operability(config)
        engine = config._cm_runtime_record[1]
    finally:
        try:
            engine = engine or create_db_engine(url, db_schema=schema)
            Base.metadata.drop_all(engine)
            Table(VERSION_TABLE, MetaData(), schema=schema).drop(engine, checkfirst=True)
            if created:
                with raw.begin() as connection:
                    connection.execute(DropSchema(schema))
        finally:
            if engine is not None: engine.dispose()
            raw.dispose()
