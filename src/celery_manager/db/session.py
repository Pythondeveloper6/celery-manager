from __future__ import annotations

import logging

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.migrate import migration_status, upgrade_database

logger = logging.getLogger(__name__)


def create_db_engine(db_url: str) -> Engine:
    """The engine celery-manager uses for `db_url`, with the SQLite
    adjustments every process needs. Creates no tables."""
    if db_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if ":memory:" in db_url:
            # SQLAlchemy's default pool for sqlite ":memory:" URLs
            # (SingletonThreadPool) hands each thread its own separate
            # in-memory database. HTTP clients such as FastAPI's TestClient
            # dispatch requests on a worker thread, so without StaticPool a
            # table created on the calling thread would be invisible to the
            # request handler. StaticPool keeps a single shared connection
            # for the whole engine, matching sqlite's on-disk semantics.
            return create_engine(db_url, connect_args=connect_args, poolclass=StaticPool)
        return create_engine(db_url, connect_args=connect_args)
    return create_engine(db_url)


def init_db(config: CeleryManagerConfig) -> Engine:
    """Create the engine and, unless `config.auto_migrate` is False, bring the
    schema to the latest revision (see `celery_manager.db.migrate`).

    With `auto_migrate=False` the database is not touched; a schema that
    isn't current is only logged, and running `celery-manager migrate` is
    the deployment's job."""
    engine = create_db_engine(config.db_url)
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
