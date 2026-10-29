"""Upgrade/rollback preservation against each configured disposable SQL server."""
import os

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect
from sqlalchemy.schema import CreateSchema, DropSchema

from celery_manager.db.migrate import VERSION_TABLE
from celery_manager.db.models import Base
from celery_manager.db.session import create_db_engine
from tests.release_upgrade import exercise_release_upgrade

pytestmark = pytest.mark.skipif(not os.environ.get("CM_STORAGE_DB_URL"), reason="needs disposable SQL database")


def test_previous_release_upgrade_and_rollback_preserve_data():
    url = os.environ["CM_STORAGE_DB_URL"]
    configured = os.environ.get("CM_STORAGE_SCHEMA")
    schema = configured + "_upgrade" if configured else None
    raw = create_engine(url)
    engine = None
    created_schema = False
    try:
        if schema:
            with raw.begin() as connection:
                if not inspect(connection).has_schema(schema):
                    connection.execute(CreateSchema(schema))
                    created_schema = True
        engine = create_db_engine(url, db_schema=schema)
        exercise_release_upgrade(engine, schema)
    finally:
        try:
            if engine is not None:
                Base.metadata.drop_all(engine)
                Table(VERSION_TABLE, MetaData(), schema=schema).drop(engine, checkfirst=True)
            if created_schema:
                with raw.begin() as connection:
                    connection.execute(DropSchema(schema))
        finally:
            if engine is not None:
                engine.dispose()
            raw.dispose()
