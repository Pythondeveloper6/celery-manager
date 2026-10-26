"""A change feed over `task_runs`, read by polling `updated_at`.

Why this exists: the dashboard's live updates (`/api/stream`) fan out from an
in-memory `EventBus` in the API process, but the writes worth announcing
happen in *other* processes — the events listener (`celery-manager events`)
and the workers (`@track`, `report_progress()`). Those cannot reach the API
process's memory. The database is the one thing every process already
shares, so each API process polls it: "which rows changed since I last
looked?". No broker, no extra infrastructure, and it works with any number
of API processes because each one reads the same table.

The cursor is `(updated_at, id)`, compared as a pair, so a burst of rows
sharing one timestamp (MySQL's `DATETIME` has whole-second precision; SQLite
and PostgreSQL microseconds) is paged through without skipping any.

What a pure "greater than the cursor" scan would still miss is a
*late commit*: `updated_at` is stamped when a writer flushes, not when it
commits, so a transaction stamped T1 that commits after one stamped T2 > T1
lands behind a cursor already at T2. Writers on different hosts with
slightly skewed clocks look the same. So every poll also re-reads a short
window *behind* the cursor (`OVERLAP`) and publishes anything in it not
already published at that exact `(id, updated_at)` — the `_seen` map is the
dedupe that makes the overlap safe to re-read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from celery_manager.db.models import TaskRun

# How far behind the cursor each poll looks again for late commits. Generous
# against a slow transaction or a skewed clock, cheap because the window is
# an index range scan over rows changed in the last few seconds.
OVERLAP = timedelta(seconds=5)

# Rows read per query. A poll keeps paging forward until it is caught up or
# has read MAX_PAGES_PER_POLL pages, so a burst is drained over a few polls
# instead of in one unbounded query.
BATCH_SIZE = 500
MAX_PAGES_PER_POLL = 10

_COLUMNS = (TaskRun.id, TaskRun.task_id, TaskRun.status, TaskRun.updated_at)


def _event(row) -> dict:
    updated_at = row.updated_at
    if updated_at.tzinfo is None:
        # SQLite drops tzinfo on round-trip; every value we write is UTC.
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return {
        "type": "task-updated",
        "task_id": row.task_id,
        "status": row.status,
        "updated_at": updated_at.isoformat(),
    }


class RunChangeFeed:
    """Stateful reader of `task_runs` changes. Not thread-safe: one feed per
    poller thread. `poll()` returns the events to publish since the previous
    call; the very first call only positions the cursor at the current head
    (a subscriber that just connected fetches the current state itself — it
    wants changes from now on, not a replay of history)."""

    def __init__(self, batch_size: int = BATCH_SIZE, overlap: timedelta = OVERLAP) -> None:
        self._batch_size = batch_size
        self._overlap = overlap
        self._primed = False
        # The newest (updated_at, id) published so far; None while the table
        # is empty. Values come from the database, never from this process's
        # clock, so the API host's clock is irrelevant.
        self._cursor: tuple[datetime, int] | None = None
        self._seen: dict[int, datetime] = {}

    def poll(self, session) -> list[dict]:
        if not self._primed:
            self._prime(session)
            return []

        events: list[dict] = []
        if self._cursor is not None:
            # Late commits behind the cursor, already-published rows skipped.
            for row in self._behind_cursor(session):
                if self._remember(row):
                    events.append(_event(row))

        for _ in range(MAX_PAGES_PER_POLL):
            rows = self._ahead_of_cursor(session)
            for row in rows:
                self._cursor = (row.updated_at, row.id)
                if self._remember(row):
                    events.append(_event(row))
            if len(rows) < self._batch_size:
                break

        self._prune()
        return events

    def _prime(self, session) -> None:
        head = session.execute(
            select(TaskRun.updated_at, TaskRun.id)
            .order_by(TaskRun.updated_at.desc(), TaskRun.id.desc())
            .limit(1)
        ).first()
        self._cursor = (head.updated_at, head.id) if head is not None else None
        if self._cursor is not None:
            # Everything already in the overlap window counts as seen, or the
            # first real poll would announce it as a "late commit".
            for row in self._behind_cursor(session):
                self._remember(row)
        self._primed = True

    def _behind_cursor(self, session):
        cursor_ts, cursor_id = self._cursor
        return session.execute(
            select(*_COLUMNS)
            .where(
                TaskRun.updated_at >= cursor_ts - self._overlap,
                or_(
                    TaskRun.updated_at < cursor_ts,
                    and_(TaskRun.updated_at == cursor_ts, TaskRun.id <= cursor_id),
                ),
            )
            .order_by(TaskRun.updated_at, TaskRun.id)
            .limit(self._batch_size)
        ).all()

    def _ahead_of_cursor(self, session):
        query = select(*_COLUMNS)
        if self._cursor is not None:
            cursor_ts, cursor_id = self._cursor
            query = query.where(
                or_(
                    TaskRun.updated_at > cursor_ts,
                    and_(TaskRun.updated_at == cursor_ts, TaskRun.id > cursor_id),
                )
            )
        return session.execute(
            query.order_by(TaskRun.updated_at, TaskRun.id).limit(self._batch_size)
        ).all()

    def _remember(self, row) -> bool:
        """Record `row` as published; False if it already was, at this exact
        `updated_at` (a newer write to the same row is a new event)."""
        if self._seen.get(row.id) == row.updated_at:
            return False
        self._seen[row.id] = row.updated_at
        return True

    def _prune(self) -> None:
        if self._cursor is None:
            return
        horizon = self._cursor[0] - self._overlap
        self._seen = {row_id: ts for row_id, ts in self._seen.items() if ts >= horizon}
