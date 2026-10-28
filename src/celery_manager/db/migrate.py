"""Schema migrations for celery-manager's own tables, via Alembic.

The revisions live in `celery_manager/db/migrations/versions/` and ship in
the wheel; Alembic is configured here in code, so users need no
`alembic.ini`. Entry points:

- `upgrade_database(engine)`: bring a database to the latest revision.
  Called by `init_db()` unless `CeleryManagerConfig.auto_migrate` is False,
  and by `celery-manager migrate`.
- `migration_status(engine)`: where a database is versus the latest
  revision, without changing anything.

Every database state celery-manager has ever produced is handled:

- empty: every revision runs from the start;
- created by 0.1.0a1-a3 (all tables, no version table): the baseline
  revision finds every table already there and only versions it, then the
  rest run;
- created by a pre-release 0.1.0a4 build whose `init_db()` added the
  schedule-pause columns itself (no version table, `paused_at`/`paused_by`
  present): stamped at the revision that adds them, then upgraded;
- already versioned: upgraded (a no-op when current).

The API process, Celery workers and the events listener all call
`init_db()`, often at the same moment, so the check-and-upgrade runs under
a lock: an advisory lock on PostgreSQL/MySQL, a write transaction
(`BEGIN IMMEDIATE`) on SQLite. Whoever gets it second finds the work done.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event, inspect, text
from sqlalchemy.pool import NullPool

from celery_manager.db.dialect import schema_of

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Not Alembic's default `alembic_version`: celery-manager's tables often
# live in the host application's database, which may have its own history.
VERSION_TABLE = "celery_manager_alembic_version"

# Historical revisions, fixed forever; new revisions never need to be
# listed here.
BASELINE_REVISION = "0001"  # schema of 0.1.0a1-0.1.0a3 (identical in all three)
SCHEDULE_PAUSE_REVISION = "0002"  # + periodic_schedules.paused_at / paused_by

# Tables that existed in the baseline. Any of them present without a version
# table means a database created by `create_all` before migrations existed.
_BASELINE_TABLES = frozenset(
    {"task_runs", "periodic_schedules", "schedule_overrides", "users", "app_settings", "audit_log"}
)

# Arbitrary, fixed key for PostgreSQL's advisory lock (a signed 64-bit int).
_PG_LOCK_KEY = 0x63656C6D  # "celm"
_MYSQL_LOCK_NAME = "celery_manager_migrate"
_LOCK_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class MigrationStatus:
    current: str | None  # None: unversioned (empty, or created before migrations)
    head: str
    legacy: bool  # celery-manager tables exist but carry no version table

    @property
    def up_to_date(self) -> bool:
        return self.current == self.head


def alembic_config(connection: Connection | None = None) -> Config:
    """An Alembic `Config` for celery-manager's bundled migrations. With a
    `connection`, `env.py` runs on it (inside whatever transaction it holds)
    instead of opening its own."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None, "celery-manager ships at least one revision"
    return head


def _current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE, "version_table_schema": schema_of(connection)}).get_current_revision()


def _has_tables(connection: Connection) -> bool:
    return bool(set(inspect(connection).get_table_names(schema=schema_of(connection))) & _BASELINE_TABLES)


def _adoption_stamp(connection: Connection) -> str | None:
    """For an unversioned database: the revision to stamp it at before
    upgrading, or None to run every revision from the start.

    Only one pre-migrations state is stamped: a complete schema that already
    has the pause columns (a pre-release 0.1.0a4 build's `init_db()` added
    them) is recorded at 0002 directly. Everything else, empty or a
    0.1.0a1-a3 database, runs from the start: revision 0001 creates only the
    tables that are missing and 0002 adds only the columns that are missing,
    which also brings a partial or half-upgraded database level.
    """
    inspector = inspect(connection)
    tables = set(inspector.get_table_names(schema=schema_of(connection)))
    if not _BASELINE_TABLES <= tables:
        return None
    columns = {c["name"] for c in inspector.get_columns("periodic_schedules", schema=schema_of(connection))}
    if {"paused_at", "paused_by"} <= columns:
        return SCHEDULE_PAUSE_REVISION
    return None


def migration_status(engine: Engine) -> MigrationStatus:
    """Report where `engine`'s database is relative to the latest revision.
    Read-only."""
    with engine.connect() as connection:
        current = _current_revision(connection)
        legacy = current is None and _has_tables(connection)
    return MigrationStatus(current=current, head=head_revision(), legacy=legacy)


def _upgrade_on(connection: Connection) -> None:
    """Stamp an unversioned database if needed, then upgrade to head. Runs
    inside the caller's transaction, holding the migration lock."""
    config = alembic_config(connection)
    if _current_revision(connection) is None and _has_tables(connection):
        stamp = _adoption_stamp(connection)
        logger.info(
            "celery-manager: adopting an existing database created before schema migrations (at %s).",
            stamp or "the 0.1.0a1-a3 baseline",
        )
        if stamp is not None:
            command.stamp(config, stamp)
    command.upgrade(config, "head")


def _is_sqlite_memory(engine: Engine) -> bool:
    database = engine.url.database or ""
    return database in ("", ":memory:") or "mode=memory" in str(engine.url)


def _sqlite_writer_engine(engine: Engine) -> Engine:
    """A throwaway engine on the same SQLite file whose transactions start
    with `BEGIN IMMEDIATE`, so the whole check-and-upgrade holds SQLite's
    write lock: a second process blocks (up to the timeout) and then finds
    the database already at head. pysqlite's own transaction handling is
    switched off so it can't defer or split that transaction."""
    writer = create_engine(
        engine.url,
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": _LOCK_TIMEOUT_SECONDS},
    )

    @event.listens_for(writer, "connect")
    def _no_pysqlite_transactions(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(writer, "begin")
    def _begin_immediate(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return writer


def upgrade_database(engine: Engine) -> None:
    """Bring `engine`'s database to the latest revision, adopting an
    unversioned (pre-migrations) database first. Safe to call from several
    processes at once, and a no-op when already current."""
    dialect = engine.dialect.name
    if dialect == "sqlite":
        if _is_sqlite_memory(engine):
            # One private database per engine: nobody to race with, and the
            # data only exists on this engine's (StaticPool) connection.
            with engine.begin() as connection:
                _upgrade_on(connection)
            return
        writer = _sqlite_writer_engine(engine)
        try:
            with writer.begin() as connection:
                _upgrade_on(connection)
        finally:
            writer.dispose()
        return

    with engine.connect() as connection:
        if dialect == "postgresql":
            # Transaction-scoped: released by the commit below.
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _PG_LOCK_KEY})
        elif dialect == "mssql":
            acquired = connection.execute(text(
                "DECLARE @rc int; EXEC @rc = sp_getapplock @Resource=:name, "
                "@LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=:timeout; SELECT @rc"
            ), {"name": "celery_manager_migrate:" + (schema_of(connection) or "default"),
                "timeout": _LOCK_TIMEOUT_SECONDS * 1000}).scalar()
            if acquired is None or acquired < 0:
                raise RuntimeError("celery-manager: timed out waiting for another process's schema migration")
        elif dialect in ("mysql", "mariadb"):
            acquired = connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": _MYSQL_LOCK_NAME, "timeout": _LOCK_TIMEOUT_SECONDS},
            ).scalar()
            if acquired != 1:
                raise RuntimeError("celery-manager: timed out waiting for another process's schema migration")
        try:
            _upgrade_on(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            if dialect in ("postgresql", "mysql", "mariadb", "mssql"):
                raise
            # No lock on this backend: another process may have run the same
            # migration concurrently and won. Fine if the database is current.
            if not migration_status(engine).up_to_date:
                raise
            logger.info("celery-manager: schema migrated concurrently by another process.")
        finally:
            if dialect in ("mysql", "mariadb"):
                connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": _MYSQL_LOCK_NAME})
                connection.commit()
