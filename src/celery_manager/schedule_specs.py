"""Schedule specs: an operator's edit of a beat entry's timing, as data.

A spec is the small JSON shape the dashboard sends and `periodic_schedules.
schedule_override` stores::

    {"type": "crontab", "minute": "0", "hour": "*/2", "day_of_week": "*",
     "day_of_month": "*", "month_of_year": "*", "timezone": "Europe/Berlin"}
    {"type": "interval", "seconds": 900}

``timezone`` is optional. It is an IANA name; when present a crontab is read
in that zone instead of the Celery app's ``timezone``, so two schedules of
one task can follow different business hours.

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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


class _TzCrontab(crontab):
    """A crontab whose ``tz`` is explicit instead of the app's timezone.

    Celery's ``BaseSchedule.tz`` is a cached property over ``app.timezone``.
    Assigning an instance attribute after construction would work, but a
    property keeps it correct for code that copies the schedule and reads
    ``tz`` before touching ``app``.
    """

    def __init__(self, *args, tz=None, **kwargs):
        self._cm_timezone = tz
        super().__init__(*args, **kwargs)

    def __copy__(self):
        # `copy.copy` goes through the base class's reduce protocol, which
        # re-evaluates the cached `tz` and drops our marker. Occurrence
        # projection copies schedules to pin `nowfun`, so keep both.
        clone = type(self).__new__(type(self))
        clone.__dict__.update(self.__dict__)
        return clone

    @property
    def tz(self):
        return self._cm_timezone or super().tz

    def remaining_delta(self, last_run_at, tz=None, **kwargs):
        # Beat passes UTC timestamps while the cron fields describe wall time
        # in the entry's zone. Keep Celery's calendar calculation, including
        # its starting-deadline behavior, and convert both inputs first.
        from copy import copy

        local = copy(self)
        now = self.now()
        local.nowfun = lambda: now.astimezone(self.tz)
        last = last_run_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return crontab.remaining_delta(local, last.astimezone(self.tz), tz=tz, **kwargs)

    def __reduce__(self):
        # RedBeat and shelve persist schedules using pickle. Celery's default
        # reduce drops subclass attributes, silently losing the entry zone.
        return (_restore_zoned_crontab, (
            {field: getattr(self, f"_orig_{field}") for field in CRONTAB_FIELDS},
            str(self.tz),
        ))


def _restore_zoned_crontab(fields, zone):
    return _TzCrontab(**fields, tz=ZoneInfo(zone))

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
        unknown = set(data) - {"type", "timezone", *CRONTAB_FIELDS}
        if unknown:
            raise ScheduleSpecError(f"Unknown crontab field: {', '.join(sorted(unknown))}.")
        spec = {"type": "crontab"}
        for field in CRONTAB_FIELDS:
            spec[field] = _field_text(data.get(field), _FIELD_LABELS[field])
        timezone_name = data.get("timezone")
        if timezone_name not in (None, ""):
            if not isinstance(timezone_name, str):
                raise ScheduleSpecError("The timezone must be an IANA name such as Europe/Berlin.")
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ScheduleSpecError(f"{timezone_name!r} is not a known IANA timezone.") from exc
            spec["timezone"] = timezone_name
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
    schedule_class = crontab
    if spec.get("timezone"):
        schedule_class = _TzCrontab
        kwargs["tz"] = ZoneInfo(spec["timezone"])
    try:
        return schedule_class(app=app, **kwargs)
    except Exception as exc:  # ValueError, or Celery's ParseException (not a ValueError)
        raise ScheduleSpecError(_crontab_message(kwargs, exc)) from exc


def describe_spec(spec: dict) -> str:
    """A readable sentence for a canonical spec.

    Uses ``cron-descriptor`` when it is installed (an optional, maintained
    dependency) and a small deterministic fallback otherwise. The fallback
    never guesses: an expression it can't phrase is shown as five-field cron.
    """
    if spec["type"] == "interval":
        seconds = int(spec["seconds"]) if float(spec["seconds"]).is_integer() else spec["seconds"]
        for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
            if seconds >= size and seconds % size == 0:
                count = seconds // size
                return f"Every {count} {unit}{'' if count == 1 else 's'}"
        return f"Every {seconds} second{'' if seconds == 1 else 's'}"
    expression = " ".join(
        str(spec[field])
        for field in ("minute", "hour", "day_of_month", "month_of_year", "day_of_week")
    )
    try:
        from cron_descriptor import get_description
        text = get_description(expression)
    except Exception:  # noqa: BLE001 - optional dependency, or a spec it can't phrase
        text = ""
    if text:
        return text
    day = spec["day_of_week"]
    if spec["day_of_month"] == "*" and spec["month_of_year"] == "*":
        names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        try:
            days = ("every day" if day == "*" else
                    "Monday through Friday" if day == "1-5" else
                    ", ".join(names[int(d)] for d in day.split(",")))
            minute = int(spec["minute"])
            hours = [int(h) for h in spec["hour"].split(",")]
            return "At " + " and ".join(f"{h:02d}:{minute:02d}" for h in hours) + ", " + days
        except (ValueError, IndexError):
            pass
    if spec["minute"] == "0" and spec["hour"] == "*":
        if day == "*":
            return "Every hour, on the hour"
        return f"Every hour, on the hour, on days {day}"
    return f"Cron {expression}"


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
        timezone_name = getattr(celery_schedule, "_cm_timezone", None)
        if timezone_name:
            spec["timezone"] = str(timezone_name)
        return spec
    return None
