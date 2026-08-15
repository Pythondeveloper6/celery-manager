from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import Base


def init_db(config: CeleryManagerConfig) -> Engine:
    """Create the engine and ensure all tables exist."""
    connect_args = {"check_same_thread": False} if config.db_url.startswith("sqlite") else {}
    engine = create_engine(config.db_url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
