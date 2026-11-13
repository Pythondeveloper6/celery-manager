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

import random
import time

from sqlalchemy import insert, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from celery_manager.db.models import TaskRun

DEADLOCK_ATTEMPTS = 5


def is_deadlock(exc: BaseException) -> bool:
    """True when the database chose this transaction as a deadlock victim.

    Covers MySQL/MariaDB (1213), PostgreSQL (40P01) and the SQLSTATE 40001
    serialization failure, and SQL Server's "deadlock victim" message. These
    are the errors the databases themselves document as "restart the
    transaction"; nothing else is treated as retryable.
    """
    if not isinstance(exc, DBAPIError):
        return False
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    if args and args[0] == 1213:
        return True
    if (getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)) in ("40P01", "40001"):
        return True
    return "deadlock" in str(orig).lower()


def commit_with_deadlock_retry(session, work, attempts: int = DEADLOCK_ATTEMPTS):
    """Run `work()` and commit, restarting the whole transaction on a deadlock.

    Every `task_runs` write bumps the indexed `updated_at` (the change feed's
    cursor), so concurrent writers on InnoDB contend on the end of that index
    and the engine kills one of them with error 1213. That is a normal,
    documented outcome — the application must restart the transaction — not a
    bug in either writer, and losing the event or the run's final state to it
    would be wrong. `work` must therefore be safe to run again from the top
    (it is handed a freshly rolled-back session); anything with side effects
    outside the database belongs before this call. A deadlock that persists
    through `attempts` tries, and every other error, propagates unchanged.
    """
    for attempt in range(1, attempts + 1):
        try:
            result = work()
            session.commit()
            return result
        except DBAPIError as exc:
            if attempt == attempts or not is_deadlock(exc):
                raise
            session.rollback()
            time.sleep(random.uniform(0, 0.02 * 2**attempt))
    raise AssertionError("unreachable")  # pragma: no cover


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
        .with_hint(TaskRun, "WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql")
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

    # SQLite takes its database write lock with the insert. InnoDB must also
    # insert first: concurrent SELECT FOR UPDATE reads of the same missing
    # key can hold compatible gap locks, then deadlock upgrading for INSERT.
    # Native ON DUPLICATE KEY UPDATE acquires the write lock directly.
    # PostgreSQL and other backends select first to avoid consuming a sequence
    # value on every update; SQL Server needs its absent-key range lock.
    if dialect_name not in ("sqlite", "mysql", "mariadb"):
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
