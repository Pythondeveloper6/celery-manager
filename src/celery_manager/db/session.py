from __future__ import annotations

import logging

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.migrate import migration_status, upgrade_database

logger = logging.getLogger(__name__)


def create_db_engine(db_url: str, *, db_schema=None, engine_options=None) -> Engine:
    """The engine celery-manager uses for `db_url`, with the SQLite
    adjustments every process needs. Creates no tables."""
    import re
    if db_schema is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", db_schema):
        raise ValueError("db_schema must be a SQL identifier")
    options = dict(engine_options or {})
    if db_schema:
        execution = dict(options.get("execution_options") or {})
        execution["schema_translate_map"] = {None: db_schema}
        options["execution_options"] = execution
    if db_url.startswith("sqlite"):
        if db_schema not in (None, "main"):
            raise ValueError("SQLite does not support a dedicated schema; use a separate database file")
        connect_args = {"check_same_thread": False, **options.pop("connect_args", {})}
        if ":memory:" in db_url:
            # SQLAlchemy's default pool for sqlite ":memory:" URLs
            # (SingletonThreadPool) hands each thread its own separate
            # in-memory database. HTTP clients such as FastAPI's TestClient
            # dispatch requests on a worker thread, so without StaticPool a
            # table created on the calling thread would be invisible to the
            # request handler. StaticPool keeps a single shared connection
            # for the whole engine, matching sqlite's on-disk semantics.
            return create_engine(db_url, connect_args=connect_args, **{"poolclass": StaticPool, **options})
        return create_engine(db_url, connect_args=connect_args, **options)
    return create_engine(db_url, **options)


def init_db(config: CeleryManagerConfig) -> Engine:
    """Create the engine and, unless `config.auto_migrate` is False, bring the
    schema to the latest revision (see `celery_manager.db.migrate`).

    With `auto_migrate=False` the database is not touched; a schema that
    isn't current is only logged, and running `celery-manager migrate` is
    the deployment's job."""
    engine = create_db_engine(config.db_url, db_schema=config.db_schema, engine_options=config.engine_options)
    if config.auto_migrate:
        upgrade_database(engine)
        return engine
    try:
        status = migration_status(engine)
    except Exception:  # a status check must never block startup
        logger.warning("celery-manager: could not read the schema revision.", exc_info=True)
        return engine
    if not status.up_to_date:
        logger.warning(
            "celery-manager: database schema is at revision %s but this version expects %s, and "
            "auto_migrate is off. Run `celery-manager migrate --db-url ...` before using it.",
            status.current or ("unversioned" if status.legacy else "empty"),
            status.head,
        )
    return engine


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
