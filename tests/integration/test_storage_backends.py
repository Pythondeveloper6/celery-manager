"""Real database release gates. CI supplies an isolated, disposable database."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.schema import CreateSchema, DropSchema

from celery_manager import CeleryManagerConfig
from celery_manager.control.retention import RetentionRules, run_prune
from celery_manager.db.migrate import VERSION_TABLE, migration_status
from celery_manager.db.models import Base, TaskRun
from celery_manager.db.runs import lock_task_run
from celery_manager.db.session import get_session_factory, init_db

pytestmark = pytest.mark.skipif(not os.environ.get("CM_STORAGE_DB_URL"), reason="needs a disposable SQL database")


@pytest.fixture(scope="module")
def database():
    url = os.environ["CM_STORAGE_DB_URL"]
    schema = os.environ.get("CM_STORAGE_SCHEMA") or None
    raw = create_engine(url)
    if schema:
        with raw.begin() as conn:
            if not inspect(conn).has_schema(schema):
                conn.execute(CreateSchema(schema))
    config = CeleryManagerConfig(celery_app=None, db_url=url, db_schema=schema)
    yield config
    engine = create_engine(url, execution_options={"schema_translate_map": {None: schema}} if schema else {})
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        preparer = conn.dialect.identifier_preparer
        version = (preparer.quote_schema(schema) + "." if schema else "") + preparer.quote(VERSION_TABLE)
        conn.exec_driver_sql("DROP TABLE " + version)
        if schema:
            conn.execute(DropSchema(schema))
    engine.dispose()
    raw.dispose()


def test_eight_concurrent_migrations_converge(database):
    def migrate(_):
        engine = init_db(database)
        try:
            return migration_status(engine).up_to_date
        finally:
            engine.dispose()
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(migrate, range(8)))


def test_namespaced_writes_utc_defaults_and_batched_prune(database):
    engine = init_db(database)
    sessions = get_session_factory(engine)
    old = datetime.now(timezone.utc) - timedelta(days=60)
    try:
        assert "task_runs" in inspect(engine).get_table_names(schema=database.db_schema)
        with sessions() as session:
            run = lock_task_run(session, "storage-contract", seed={"task_name": "contract", "status": "succeeded"})
            run.finished_at = old
            run.result_repr = '{"done": true}'
            session.commit()
            summary = run_prune(session, rules=RetentionRules(default_days=30), batch_size=1)
            assert summary["runs_deleted"] == 1
            assert session.query(TaskRun).filter_by(task_id="storage-contract").count() == 0
        # Omit the Python default deliberately: test the database's UTC clock.
        values = {"task_id": "server-default", "task_name": "contract", "status": "queued", "args": "[]",
                  "kwargs": "{}", "retry_count": 0, "log_output_truncated": False, "result_truncated": False}
        with engine.begin() as conn:
            # Inline SQL avoids SQLAlchemy applying its Python-side default.
            preparer = conn.dialect.identifier_preparer
            name = (preparer.quote_schema(database.db_schema) + "." if database.db_schema else "") + preparer.quote("task_runs")
            from sqlalchemy import text
            statement = "INSERT INTO " + name + " (" + ",".join(values) + ") VALUES (" + ",".join(":" + c for c in values) + ")"
            conn.execute(text(statement), values)
        with sessions() as session:
            stamp = session.query(TaskRun).filter_by(task_id="server-default").one().updated_at
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 30
    finally:
        engine.dispose()
