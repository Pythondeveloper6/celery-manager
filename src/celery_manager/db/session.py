from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import Base


def init_db(config: CeleryManagerConfig) -> Engine:
    """Create the engine and ensure all tables exist."""
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
            return engine
    else:
        connect_args = {}
    engine = create_engine(config.db_url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
