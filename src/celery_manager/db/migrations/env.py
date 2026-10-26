"""Alembic environment for celery-manager's own tables.

Two ways in:

- **Programmatic** (every real use): `celery_manager.db.migrate` builds an
  in-memory `alembic.config.Config` and hands over an open connection in
  `config.attributes["connection"]`, already inside the transaction (and
  holding the cross-process lock) the migration must run in. Committing is
  the caller's job.
- **The `alembic` CLI** (contributors only): the repo-root `alembic.ini`
  points here with a scratch SQLite URL, overridable with
  `-x db_url=...`.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from celery_manager.db.migrate import VERSION_TABLE
from celery_manager.db.models import Base

config = context.config
target_metadata = Base.metadata

if config.config_file_name is not None and config.attributes.get("connection") is None:
    # Only for the contributor CLI: never reconfigure a host application's
    # logging from inside init_db().
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _include_name(name, type_, parent_names) -> bool:
    # Autogenerate only ever considers celery-manager's own tables, so a
    # revision generated against a database shared with the host app never
    # proposes dropping the host app's tables.
    if type_ == "table":
        return name in target_metadata.tables
    return True


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        # Our own version table rather than Alembic's default
        # `alembic_version`: celery-manager often shares a database with the
        # host application, which may run its own Alembic history there.
        version_table=VERSION_TABLE,
        # SQLite can't ALTER most things in place; batch mode recreates the
        # table instead. On other backends batch operations are plain ALTERs.
        render_as_batch=True,
        compare_type=True,
        include_name=_include_name,
        **kwargs,
    )


def _url() -> str:
    return context.get_x_argument(as_dictionary=True).get("db_url") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            _configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
