"""Schedule specs: an operator's edit of a beat entry's timing, as data.

A spec is the small JSON shape the dashboard sends and `periodic_schedules.
schedule_override` stores::

    {"type": "crontab", "minute": "0", "hour": "*/2", "day_of_week": "*",
     "day_of_month": "*", "month_of_year": "*"}
    {"type": "interval", "seconds": 900}

Shared by the API (validate, preview, project occurrences) and the beat
scheduler (`celery_manager.beat.DatabaseScheduler`, which builds the same
schedule object from the stored text), so both sides read one stored edit
the same way. Like `celery_manager.occurrences`, deliberately free of
database imports.

Crontab fields are parsed by Celery's own `crontab(...)`, so anything Celery
accepts in a `beat_schedule` is accepted here and nothing else is.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from celery.schedules import crontab
from celery.schedules import schedule as interval_schedule

from celery_manager.occurrences import is_interval_schedule, next_occurrence_after

CRONTAB_FIELDS = ("minute", "hour", "day_of_week", "day_of_month", "month_of_year")

_FIELD_LABELS = {
    "minute": "Minute",
    "hour": "Hour",
    "day_of_week": "Day of week",
    "day_of_month": "Day of month",
    "month_of_year": "Month",
}

# An interval shorter than a second is a busy loop, not a schedule: beat
# would fire it on every tick. The upper bound only keeps the datetime
# arithmetic of a projection far away from overflow; nobody schedules a
# task every two years through a dashboard.
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 366 * 24 * 3600


class ScheduleSpecError(ValueError):
    """An invalid spec. The message is written for a person: the API returns
    it as-is in a 422 and the dashboard shows it under the form."""


def _field_text(value, label: str) -> str:
    if value is None:
        return "*"
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ScheduleSpecError(f"{label} must be text such as '*', '0' or '*/15'.")
    text = str(value).strip()
    return text or "*"


def normalize_spec(data) -> dict:
    """Check the shape of `data` and return the canonical spec (missing
    crontab fields become ``"*"``, interval seconds a number). Does not yet
    ask Celery whether the crontab parses: `validate_spec` does that."""
    if not isinstance(data, dict):
        raise ScheduleSpecError("Send the schedule as a JSON object.")
    kind = data.get("type")
    if kind == "crontab":
        unknown = set(data) - {"type", *CRONTAB_FIELDS}
        if unknown:
            raise ScheduleSpecError(f"Unknown crontab field: {', '.join(sorted(unknown))}.")
        spec = {"type": "crontab"}
        for field in CRONTAB_FIELDS:
            spec[field] = _field_text(data.get(field), _FIELD_LABELS[field])
        return spec
    if kind == "interval":
        unknown = set(data) - {"type", "seconds"}
        if unknown:
            raise ScheduleSpecError(f"Unknown interval field: {', '.join(sorted(unknown))}.")
        seconds = data.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            raise ScheduleSpecError("Enter the interval as a number of seconds.")
        if seconds < MIN_INTERVAL_SECONDS:
            raise ScheduleSpecError(f"The interval must be at least {MIN_INTERVAL_SECONDS} second.")
        if seconds > MAX_INTERVAL_SECONDS:
            raise ScheduleSpecError("The interval must be at most 366 days.")
        # Whole seconds stay ints, so the stored JSON reads "900", not "900.0".
        return {"type": "interval", "seconds": int(seconds) if float(seconds).is_integer() else float(seconds)}
    raise ScheduleSpecError('The schedule type must be "crontab" or "interval".')


def build_schedule(spec: dict, app=None):
    """The `celery.schedules` object for a canonical spec.

    `app` sets the timezone a crontab is read in (the Celery app's
    `timezone`, as beat reads the configured entries). The object is new
    and ours, so attaching the app mutates nothing shared."""
    if spec["type"] == "interval":
        return interval_schedule(run_every=timedelta(seconds=spec["seconds"]), app=app)
    kwargs = {field: spec[field] for field in CRONTAB_FIELDS}
    try:
        return crontab(app=app, **kwargs)
    except Exception as exc:  # ValueError, or Celery's ParseException (not a ValueError)
        raise ScheduleSpecError(_crontab_message(kwargs, exc)) from exc


def _crontab_message(kwargs: dict, exc: Exception) -> str:
    """Name the field that failed. Celery's own message ("Invalid weekday
    literal 'abc'") does not say which field it was reading, and its
    weekday parser is the fallback for any field with letters in it."""
    for field in CRONTAB_FIELDS:
        try:
            crontab(**{field: kwargs[field]})
        except Exception as field_exc:  # noqa: BLE001
            detail = str(field_exc).rstrip(".") or "not a valid value"
            return f"{_FIELD_LABELS[field]} '{kwargs[field]}' is not valid: {detail}."
    return f"Not a valid crontab: {exc}."


def validate_spec(data, app=None, now: datetime | None = None):
    """Normalize `data`, build it with Celery, and check that it ever fires.

    Returns ``(spec, schedule)``. A crontab can parse and still never match
    a date (``day_of_month=31, month_of_year=2``); Celery then raises when
    asked for the next run, which beat would do on every tick, so it is
    refused here instead of being saved."""
    spec = normalize_spec(data)
    celery_schedule = build_schedule(spec, app=app)
    if not is_interval_schedule(celery_schedule):
        try:
            next_occurrence_after(celery_schedule, now or datetime.now(timezone.utc))
        except Exception as exc:  # RuntimeError("unable to rollover ...")
            raise ScheduleSpecError(
                "This crontab never fires: no date matches it (for example the 31st of February)."
            ) from exc
    return spec, celery_schedule


def dump_spec(spec: dict) -> str:
    """The stored form: canonical JSON, so equal specs compare equal as text
    (the beat scheduler compares stored text to see what changed)."""
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def load_spec(text: str | None) -> dict | None:
    """Parse a stored override. None for no override; ScheduleSpecError for
    text that is not a valid spec (written by hand, or by a newer version)."""
    if text is None or text == "":
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ScheduleSpecError("The stored schedule edit is not valid JSON.") from exc
    return normalize_spec(data)


def _orig_text(value) -> str:
    if isinstance(value, (set, frozenset, list, tuple)):
        return ",".join(str(v) for v in sorted(value))
    return str(value)


def spec_from_schedule(celery_schedule) -> dict | None:
    """The spec equivalent of a configured schedule, to prefill the editor
    with what the code says. None for kinds a spec cannot express (solar,
    custom schedule classes): the editor then starts from a blank form."""
    if is_interval_schedule(celery_schedule):
        seconds = celery_schedule.run_every.total_seconds()
        return {"type": "interval", "seconds": int(seconds) if seconds.is_integer() else seconds}
    if isinstance(celery_schedule, crontab):
        spec = {"type": "crontab"}
        for field in CRONTAB_FIELDS:
            spec[field] = _orig_text(getattr(celery_schedule, f"_orig_{field}", "*"))
        return spec
    return None
