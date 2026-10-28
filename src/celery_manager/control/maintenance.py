"""Maintenance windows: a weekly recurring span during which chosen
schedules' firings are skipped, with nothing to resume afterwards.

Why a window is *evaluated* instead of pausing and resuming schedules at its
edges: that needs a process that is alive at 02:00 on Sunday and writes
reliably at 04:00 (and a missed resume is an outage nobody notices). The
decorator instead asks, per firing, whether the occurrence falls inside an
enabled window (`active_windows`), so beat keeps sending, a restart loses
nothing, and editing or disabling a window takes effect on the next firing.

Time semantics, all computed with `zoneinfo` in the window's own timezone:

- A window starts on each chosen weekday at `start_time` local wall-clock
  time and lasts `duration_minutes` of *elapsed* time, so it can cross
  midnight and a window straddling a DST change is one hour shorter or
  longer on the wall clock, never a different length in real time.
- The start is inclusive and the end exclusive: a firing at exactly the end
  runs. A firing at exactly the start is skipped.
- A start time that does not exist on a spring-forward day (02:30 when the
  clocks jump 02:00 -> 03:00) begins at the same instant the clocks would
  have reached it, i.e. shifted forward by the gap; an ambiguous one on a
  fall-back day (01:30 twice) uses the first occurrence.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery_manager.control.audit import record as record_audit
from celery_manager.db.models import MaintenanceWindow

ALL_SCHEDULES = "*"
# Longer than a week would make consecutive weekly starts overlap, which
# the recurrence has no way to express.
MAX_DURATION_MINUTES = 7 * 24 * 60
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class MaintenanceWindowError(ValueError):
    """The window fields are invalid; the message is shown to the operator."""


class DuplicateWindowName(ValueError):
    """Another window already has this name."""


def default_timezone(celery_app) -> str:
    """The Celery app's configured timezone, or UTC when it has none."""
    name = getattr(celery_app.conf, "timezone", None)
    return str(name) if name else "UTC"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise MaintenanceWindowError(f"Unknown timezone {name!r}; use an IANA name such as Europe/Berlin.") from exc


def normalize_window(data, default_tz: str = "UTC", known_schedules=None) -> dict:
    """Validate the editable fields of a window and return them in stored
    shape. Raises `MaintenanceWindowError` with a sentence for the operator.

    `known_schedules`, when given, is the set of schedule names `applies_to`
    may name; a typo would otherwise silently protect nothing.
    """
    if not isinstance(data, dict):
        raise MaintenanceWindowError("Expected an object.")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise MaintenanceWindowError("Give the window a name.")
    name = name.strip()
    if len(name) > 255:
        raise MaintenanceWindowError("The name is too long (255 characters at most).")

    fields = normalize_recurrence(data, default_tz)

    applies = data.get("applies_to", ALL_SCHEDULES)
    if isinstance(applies, list) and applies == [ALL_SCHEDULES]:
        applies = ALL_SCHEDULES
    if applies != ALL_SCHEDULES:
        if not isinstance(applies, list) or not applies or any(not isinstance(n, str) or not n for n in applies):
            raise MaintenanceWindowError('Choose the schedules this applies to, or "*" for all of them.')
        applies = sorted(set(applies))
        if known_schedules is not None:
            unknown = [n for n in applies if n not in known_schedules]
            if unknown:
                raise MaintenanceWindowError(f"Unknown schedule(s): {', '.join(unknown)}.")

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MaintenanceWindowError("`enabled` must be true or false.")

    return {**fields, "name": name, "applies_to": applies, "enabled": enabled}


def normalize_recurrence(data, default_tz: str = "UTC") -> dict:
    """Validate just the when-part of a window (days, start, duration,
    timezone); the preview endpoint needs it without a name or schedules."""
    if not isinstance(data, dict):
        raise MaintenanceWindowError("Expected an object.")
    days = data.get("days_of_week")
    if (
        not isinstance(days, list)
        or not days
        or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)
    ):
        raise MaintenanceWindowError("Choose at least one day of the week (0 = Monday ... 6 = Sunday).")

    start = data.get("start_time")
    if not isinstance(start, str) or not _TIME_RE.match(start):
        raise MaintenanceWindowError("The start time must be HH:MM (24-hour), for example 02:00.")

    duration = data.get("duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= MAX_DURATION_MINUTES:
        raise MaintenanceWindowError(
            f"The duration must be a whole number of minutes from 1 to {MAX_DURATION_MINUTES}."
        )

    tz = data.get("timezone") or default_tz
    if not isinstance(tz, str):
        raise MaintenanceWindowError("The timezone must be an IANA name.")
    _zone(tz)

    return {"days_of_week": sorted(set(days)), "start_time": start, "duration_minutes": duration, "timezone": tz}


# --- recurrence arithmetic (pure) -------------------------------------------


def _as_utc(dt: datetime) -> datetime:
    """Naive datetimes in this codebase are UTC (SQLite drops tzinfo)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_days(row) -> list[int]:
    value = row.days_of_week
    if isinstance(value, str):
        return [int(d) for d in value.split(",") if d != ""]
    return list(value)


def _row_applies(row):
    value = row.applies_to
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value


def applies_to_schedule(row, schedule_name: str) -> bool:
    applies = _row_applies(row)
    return applies == ALL_SCHEDULES or (isinstance(applies, list) and schedule_name in applies)


def _span_starting(zone: ZoneInfo, day: date, start: str, minutes: int) -> tuple[datetime, datetime]:
    hh, mm = int(start[:2]), int(start[3:])
    begin = datetime.combine(day, time(hh, mm), tzinfo=zone).astimezone(timezone.utc)
    return begin, begin + timedelta(minutes=minutes)


def _spans_between(row, lo: datetime, hi: datetime):
    """Every (start, end) span of this window, in UTC and ascending, whose
    start is on a local day from `lo`'s day minus the window's length up to
    `hi`'s day. A superset of the spans overlapping [lo, hi]; callers filter."""
    zone = _zone(row.timezone)
    days = set(_row_days(row))
    back = row.duration_minutes // (24 * 60) + 2
    day = lo.astimezone(zone).date() - timedelta(days=back)
    last = hi.astimezone(zone).date() + timedelta(days=1)
    while day <= last:
        if day.weekday() in days:
            yield _span_starting(zone, day, row.start_time, row.duration_minutes)
        day += timedelta(days=1)


def window_span_at(row, at: datetime) -> tuple[datetime, datetime] | None:
    """The (start, end) of this window's span containing `at`, or None.
    Start inclusive, end exclusive. Ignores `enabled` and `applies_to`."""
    at = _as_utc(at)
    for start, end in _spans_between(row, at, at):
        if start <= at < end:
            return start, end
    return None


def next_spans(row, after: datetime, count: int = 5) -> list[tuple[datetime, datetime]]:
    """The next `count` spans that have not ended by `after` (so one in
    progress is included first), as UTC (start, end) pairs."""
    after = _as_utc(after)
    horizon = after
    spans: list[tuple[datetime, datetime]] = []
    # Weekly recurrence: a few weeks always hold `count` spans for a window
    # with at least one day; the cap only guards the loop.
    for _ in range(60):
        horizon += timedelta(days=28)
        spans = [s for s in _spans_between(row, after, horizon) if s[1] > after]
        if len(spans) >= count:
            break
    return spans[:count]


def active_windows(session, schedule_name: str, at: datetime) -> list[MaintenanceWindow]:
    """The enabled windows that apply to `schedule_name` and contain `at`."""
    return windows_active_at(enabled_windows(session), schedule_name, at)


def windows_active_at(rows, schedule_name: str, at: datetime) -> list:
    """`active_windows` over rows already loaded, for callers testing many
    occurrences. Skips disabled rows itself."""
    return [
        r for r in rows
        if r.enabled and applies_to_schedule(r, schedule_name) and window_span_at(r, at) is not None
    ]


def enabled_windows(session) -> list[MaintenanceWindow]:
    return (
        session.query(MaintenanceWindow)
        .filter(MaintenanceWindow.enabled.is_(True))
        .order_by(MaintenanceWindow.id)
        .all()
    )


# --- CRUD -------------------------------------------------------------------


def list_windows(session) -> list[MaintenanceWindow]:
    return session.query(MaintenanceWindow).order_by(MaintenanceWindow.name).all()


def _details(fields: dict) -> dict:
    keys = ("days_of_week", "start_time", "duration_minutes", "timezone", "applies_to", "enabled")
    return {k: fields[k] for k in keys}


def _apply(row: MaintenanceWindow, fields: dict) -> None:
    row.name = fields["name"]
    row.days_of_week = ",".join(str(d) for d in fields["days_of_week"])
    row.start_time = fields["start_time"]
    row.duration_minutes = fields["duration_minutes"]
    row.timezone = fields["timezone"]
    row.applies_to = json.dumps(fields["applies_to"])
    row.enabled = fields["enabled"]


def _name_taken(session, name: str, except_id: int | None = None) -> bool:
    q = session.query(MaintenanceWindow.id).filter(MaintenanceWindow.name == name)
    if except_id is not None:
        q = q.filter(MaintenanceWindow.id != except_id)
    return q.first() is not None


def create_window(session, data, username: str, default_tz: str = "UTC", known_schedules=None) -> MaintenanceWindow:
    fields = normalize_window(data, default_tz, known_schedules)
    if _name_taken(session, fields["name"]):
        raise DuplicateWindowName(f"A maintenance window named {fields['name']!r} already exists.")
    row = MaintenanceWindow(created_by=username, created_at=datetime.now(timezone.utc))
    _apply(row, fields)
    session.add(row)
    session.commit()
    record_audit(session, username, "create_maintenance_window", "maintenance_window", row.name, _details(fields))
    return row


def update_window(
    session, window_id: int, data, username: str, default_tz: str = "UTC", known_schedules=None
) -> MaintenanceWindow | None:
    row = session.get(MaintenanceWindow, window_id)
    if row is None:
        return None
    fields = normalize_window(data, default_tz, known_schedules)
    if _name_taken(session, fields["name"], except_id=row.id):
        raise DuplicateWindowName(f"A maintenance window named {fields['name']!r} already exists.")
    _apply(row, fields)
    row.updated_by = username
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    record_audit(session, username, "update_maintenance_window", "maintenance_window", row.name, _details(fields))
    return row


def delete_window(session, window_id: int, username: str) -> bool:
    row = session.get(MaintenanceWindow, window_id)
    if row is None:
        return False
    name = row.name
    session.delete(row)
    session.commit()
    record_audit(session, username, "delete_maintenance_window", "maintenance_window", name)
    return True


def window_fields(row: MaintenanceWindow, now: datetime | None = None) -> dict:
    """The JSON shape of one window, with whether it is in progress now and
    its next span (the one in progress, while it is)."""
    now = now or datetime.now(timezone.utc)
    upcoming = next_spans(row, now, 1)
    return {
        "id": row.id,
        "name": row.name,
        "days_of_week": _row_days(row),
        "start_time": row.start_time,
        "duration_minutes": row.duration_minutes,
        "timezone": row.timezone,
        "applies_to": _row_applies(row),
        "enabled": row.enabled,
        "created_by": row.created_by,
        "created_at": _as_utc(row.created_at) if row.created_at else None,
        "updated_by": row.updated_by,
        "updated_at": _as_utc(row.updated_at) if row.updated_at else None,
        "active": row.enabled and window_span_at(row, now) is not None,
        "next_start": upcoming[0][0] if upcoming else None,
        "next_end": upcoming[0][1] if upcoming else None,
    }
