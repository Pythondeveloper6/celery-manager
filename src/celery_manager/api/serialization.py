"""Shared response-shaping helpers for the REST layer.

One function so far, and it exists in its own module rather than as a private
helper on one router because more than one router needs it and the bug it
prevents is invisible until a browser in a non-UTC timezone renders the
response.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc(value: datetime | None) -> datetime | None:
    """Stamp UTC onto a datetime read back from the database.

    SQLite drops tzinfo on a `DateTime(timezone=True)` round-trip, so a value
    written as an aware UTC datetime (every writer in this codebase uses
    `datetime.now(timezone.utc)`) comes back naive. FastAPI then serializes it
    as "2026-08-10T00:00:00", with no offset marker — and `new Date(...)` on
    that string in a browser parses it as *local* time. For a viewer in UTC+2
    that shifts every timestamp by two hours, in the direction that makes a
    live elapsed-time counter read negative.

    So normalize at the API boundary, where the value becomes JSON, rather
    than in each caller. None passes through untouched — a run that never
    started has no start time, and inventing one would be worse than omitting
    it.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def utc_bound(value: datetime | None) -> datetime | None:
    """Normalize an inbound date-range query parameter to UTC.

    The mirror of `utc`, on the way in. Every timestamp in the database is
    stored as UTC, but SQLAlchemy's SQLite `DATETIME` binds a datetime by
    formatting its *fields* and discarding tzinfo — so
    `?date_from=2026-08-10T16:00:00%2B02:00` was compared as 16:00 rather
    than as the 14:00 UTC instant it names, silently shifting the window by
    the caller's offset. Converting first makes the fields agree with what is
    stored, and is a no-op for the backends that do understand tzinfo.

    Naive input is left exactly as-is: this codebase's standing convention is
    that a naive datetime means UTC (see `utc`), so there is nothing to
    convert. Our own SPA always sends `toISOString()`, i.e. `Z`, so this
    matters for third-party API callers rather than for the dashboard.
    """
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc)
