"""Race-safe get-or-create for `task_runs` rows.

`task_runs.task_id` is UNIQUE, and up to four independent writers create rows
for the same uuid with no coordination between them: the events listener (its
own process, from `task-sent`/`task-received`), the `@track` decorator (inside
the worker, the `started` row), `report_progress()` (for undecorated tasks),
and `retry_task` (the API process, to record lineage). Each of them used to do
check-then-insert — SELECT, see nothing, INSERT — and whoever lost the race
between the SELECT and the commit got an `IntegrityError`: a 500 from the
retry button with the lineage lost, or an events listener that dropped the
event (and the worker/queue it carried). "Check first" cannot be fixed by
checking harder; the insert itself has to tolerate the row already existing.

So `lock_task_run` *guarantees* a row exists and returns it locked, and every
writer merges its own facts into that row afterwards under its own rules.
Insert-if-absent is expressed natively where the dialect has it
(`ON CONFLICT DO NOTHING` on SQLite/PostgreSQL, a no-op `ON DUPLICATE KEY
UPDATE` on MySQL/MariaDB) and through a SAVEPOINT + `IntegrityError` fallback
everywhere else, so nothing here depends on SQLite.
"""

from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from celery_manager.db.models import TaskRun


def _insert_if_absent(dialect_name: str, values: dict):
    """A dialect-native "insert unless task_id already exists" statement, or
    None if this dialect needs the generic fallback."""
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(TaskRun).values(**values).on_conflict_do_nothing(index_elements=["task_id"])
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(TaskRun).values(**values).on_conflict_do_nothing(index_elements=["task_id"])
    if dialect_name in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        stmt = mysql_insert(TaskRun).values(**values)
        # MySQL has no DO NOTHING; assigning the key to itself is the
        # idiomatic no-op. Not `INSERT IGNORE`, which would also swallow
        # NOT NULL / truncation errors that ought to surface.
        return stmt.on_duplicate_key_update(task_id=stmt.inserted.task_id)
    return None


def _select_locked(session, task_id: str) -> TaskRun | None:
    # FOR UPDATE matters for two reasons. It serialises concurrent
    # read-modify-write merges on the same row (PostgreSQL/MySQL; SQLite
    # renders nothing here and is serialised by the insert-first ordering in
    # `lock_task_run` instead). And on MySQL's default REPEATABLE READ a
    # *plain* SELECT reads the transaction's snapshot, which can predate the
    # competing writer's committed insert — a locking read sees the latest
    # committed row. populate_existing so a stale copy already in this
    # session's identity map is refreshed rather than trusted.
    return session.execute(
        select(TaskRun)
        .where(TaskRun.task_id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def lock_task_run(session, task_id: str, seed: dict | None = None) -> TaskRun:
    """Return the `task_runs` row for `task_id`, creating it from `seed` if
    no writer has yet, without ever raising IntegrityError for a concurrent
    insert of the same uuid.

    `seed` is create-only: it is used solely when this call is the one that
    inserts the row, and `None` values in it are dropped (so the column
    defaults apply). It must carry the NOT NULL `task_name` and `status`.
    Callers merge everything else into the returned row themselves — that is
    where each writer's precedence rules live, and they differ. Deliberately
    no "was it created?" flag: MySQL's affected-rows count for ON DUPLICATE
    KEY depends on the CLIENT_FOUND_ROWS flag, so it cannot be trusted, and
    no caller needs it — "seed only on insert" is exactly what `seed` is.

    The row is locked (SELECT ... FOR UPDATE) until the caller's transaction
    ends, so callers should commit promptly.
    """
    values = {k: v for k, v in (seed or {}).items() if v is not None}
    values["task_id"] = task_id
    dialect_name = session.get_bind().dialect.name

    # Ordering differs by dialect for a reason, not by accident:
    #
    # - SQLite: insert first. Its locking is database-wide; the INSERT takes
    #   the write lock up front, so the SELECT and the caller's UPDATE that
    #   follow run serialised against every other writer (they wait on the
    #   busy timeout rather than interleaving and losing updates). Cheap, too:
    #   a conflicting DO NOTHING burns no rowid.
    # - Everything else: select first. A conflicting ON CONFLICT / ON
    #   DUPLICATE KEY still consumes a sequence / auto-increment value, and
    #   the listener alone writes ~5 events per task — inserting first would
    #   burn ids several times faster than rows are created. The row lock
    #   from FOR UPDATE provides the serialisation instead.
    if dialect_name != "sqlite":
        run = _select_locked(session, task_id)
        if run is not None:
            return run

    stmt = _insert_if_absent(dialect_name, values)
    insert_error = None
    if stmt is not None:
        session.execute(stmt)
    else:
        # Generic fallback for dialects without a native upsert. The SAVEPOINT
        # confines the failed INSERT, so the caller's surrounding transaction
        # (and anything else pending in it) survives a lost race.
        try:
            with session.begin_nested():
                session.execute(insert(TaskRun).values(**values))
        except IntegrityError as exc:
            insert_error = exc

    run = _select_locked(session, task_id)
    if run is None:
        # The insert failed for some reason other than "already exists" (a
        # NOT NULL seed missing, say) — surface that, not a misleading
        # NoResultFound.
        if insert_error is not None:
            raise insert_error
        raise LookupError(f"task_runs row for {task_id} vanished after insert")
    return run
