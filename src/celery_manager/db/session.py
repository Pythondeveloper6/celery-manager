from __future__ import annotations

import logging

from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateColumn

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import Base

logger = logging.getLogger(__name__)

# Nullable columns added to an *existing* table after it first shipped, as
# (table, column) pairs. `create_all` never alters a table that already
# exists, so without this an older database would fail every query that
# touches the table with "no such column". This is deliberately not a
# migration framework: it only ever ADDs a nullable column with no default
# — the one schema change that is safe on every backend, needs no data
# rewrite and cannot lose anything. Renames, type changes and NOT NULL
# columns still need manual DDL (see docs/configuration.md, "Schema and
# upgrades"). Append to this list when adding a nullable column to an
# existing model.
_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("periodic_schedules", "paused_at"),
    ("periodic_schedules", "paused_by"),
)


def _add_missing_columns(engine: Engine) -> None:
    """ALTER TABLE ... ADD COLUMN for each `_ADDITIVE_COLUMNS` entry the
    database does not have yet. A no-op on a fresh database (create_all
    already made them)."""
    for table_name, column_name in _ADDITIVE_COLUMNS:
        existing = {c["name"] for c in inspect(engine).get_columns(table_name)}
        if column_name in existing:
            continue
        column = Base.metadata.tables[table_name].c[column_name]
        ddl = CreateColumn(column).compile(dialect=engine.dialect)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")
            logger.info("celery-manager: added column %s.%s to an existing database.", table_name, column_name)
        except SQLAlchemyError:
            # Another process (API + worker + listener all call init_db)
            # may have added it between our inspect and our ALTER. Only
            # re-raise if the column really is still missing.
            if column_name not in {c["name"] for c in inspect(engine).get_columns(table_name)}:
                raise


def init_db(config: CeleryManagerConfig) -> Engine:
    """Create the engine, ensure all tables exist, and add any known
    nullable columns an older database is missing (`_ADDITIVE_COLUMNS`)."""
    if config.db_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if ":memory:" in config.db_url:
            # SQLAlchemy's default pool for sqlite ":memory:" URLs
            # (SingletonThreadPool) hands each thread its own separate
            # in-memory database. HTTP clients such as FastAPI's TestClient
            # dispatch requests on a worker thread, so without StaticPool a
            # table created on the calling thread would be invisible to the
            # request handler. StaticPool keeps a single shared connection
            # for the whole engine, matching sqlite's on-disk semantics.
            engine = create_engine(config.db_url, connect_args=connect_args, poolclass=StaticPool)
            Base.metadata.create_all(engine)
            _add_missing_columns(engine)
            return engine
    else:
        connect_args = {}
    engine = create_engine(config.db_url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
