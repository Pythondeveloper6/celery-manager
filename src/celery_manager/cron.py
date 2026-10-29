"""Deterministic English schedule parsing; ambiguous text is refused.

The parser is deliberately conservative: it only accepts phrases it can map
to one crontab (or one interval) exactly. Anything that would need a holiday
calendar, dependencies between schedules, or two different minutes is
refused with an explanation instead of being turned into a guessed schedule.

Grammar accepted (case-insensitive):

- ``every 30 seconds`` / ``every 5 minutes`` / ``every 2 hours`` / ``every day``
- ``every 15 minutes between 09:00 and 09:45`` (inclusive, within one hour)
- ``daily at 06:30`` / ``weekdays at 09:00`` / ``weekends at 10:15``
- ``Mon and Thu at 06:30`` / ``Monday, Wednesday and Friday at 18:00``
- ``Mon-Fri at 09:00``
- ``at 06:30 on Mon and Thu``
- ``daily at 09:00 and 17:00`` (several times, one shared minute)
- ``twice a day at 09:00 and 17:00``
- five-field cron: ``30 6 * * 1,4``

An optional ``timezone`` is carried on the spec (see
:mod:`celery_manager.schedule_specs`).
"""
from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery_manager.schedule_specs import describe_spec, validate_spec

_WEEKDAYS = {
    "monday": 1, "mon": 1,
    "tuesday": 2, "tue": 2, "tues": 2,
    "wednesday": 3, "wed": 3, "weds": 3,
    "thursday": 4, "thu": 4, "thur": 4, "thurs": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
    "sunday": 0, "sun": 0,
}

_INTERVAL_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}

_TIME = r"(\d{1,2}):(\d{2})"
_DAY_NAME = (
    r"(?:mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)"
    r"(?:day|sday|nesday|rsday|urday)?"
)
_DAYS = (
    r"(?:daily|every day|weekdays|weekends|"
    rf"{_DAY_NAME}(?:\s*(?:-|to)\s*{_DAY_NAME})?"
    rf"(?:\s*(?:,|and)\s*{_DAY_NAME})*"
    r")"
)


def _time_parts(text: str) -> tuple[int, int]:
    match = re.fullmatch(_TIME, text.strip())
    if not match:
        raise ValueError(f"{text!r} is not a time; use HH:MM (24-hour).")
    hour, minute = int(match[1]), int(match[2])
    if hour > 23 or minute > 59:
        raise ValueError(f"{text!r} is not a valid 24-hour time.")
    return hour, minute


def _day_field(text: str) -> str:
    """Celery day_of_week for a phrase such as ``Mon and Thu`` / ``Mon-Fri``."""
    value = " ".join(text.lower().replace(",", " , ").split())
    if value in ("daily", "every day", "*"):
        return "*"
    if value == "weekdays":
        return "1-5"
    if value == "weekends":
        return "0,6"
    # A range first: "mon-fri" or "monday to friday".
    range_match = re.fullmatch(
        r"([a-z]+)\s*(?:-|to)\s*([a-z]+)", value
    )
    if range_match:
        start, end = (_WEEKDAYS.get(range_match[1]), _WEEKDAYS.get(range_match[2]))
        if start is None or end is None:
            raise ValueError(f"{text!r} is not a weekday range.")
        return f"{start}-{end}" if start <= end else f"{start}-6,0-{end}"
    names = [part for part in re.split(r"\s*(?:,|and)\s*", value) if part]
    numbers: list[int] = []
    for name in names:
        number = _WEEKDAYS.get(name)
        if number is None:
            raise ValueError(
                f"{name!r} is not a weekday. Use Mon, Tue, Wed, Thu, Fri, Sat or Sun."
            )
        if number not in numbers:
            numbers.append(number)
    if not numbers:
        raise ValueError("Name at least one weekday.")
    return ",".join(str(number) for number in numbers)


def _hours(times: list[tuple[int, int]]) -> str:
    minutes = {minute for _, minute in times}
    if len(minutes) != 1:
        raise ValueError(
            "Several times in one schedule must share the same minute "
            "(for example 09:00 and 17:00). Create a second schedule for a "
            "different minute."
        )
    hours = sorted({hour for hour, _ in times})
    return ",".join(str(hour) for hour in hours)


def _split_times(text: str) -> list[tuple[int, int]]:
    parts = [part for part in re.split(r"\s*(?:,|and)\s*", text.strip()) if part]
    if not parts:
        raise ValueError("Name at least one time, such as 06:30.")
    return [_time_parts(part) for part in parts]


def _with_timezone(spec: dict, tz: str | None) -> dict:
    if tz is None:
        return spec
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{tz!r} is not a known IANA timezone.") from exc
    spec["timezone"] = tz
    return spec


class RuleBasedParser:
    """Deterministic parser used before any host-supplied AI provider."""

    def parse(self, text: str, tz: str = "UTC") -> dict:
        raw = " ".join(text.strip().split())
        if not raw:
            raise ValueError("Describe when the task should run.")
        # Take the zone off the raw text: IANA names are case-sensitive, and
        # lowercasing first would fold Europe/Berlin into a name that only
        # resolves on case-insensitive filesystems.
        requested_tz = None
        trailing = re.search(r"\s+in\s+([^\s]+)$", raw, re.IGNORECASE)
        if trailing:
            requested_tz, raw = trailing[1], raw[: trailing.start()]
        value = raw.lower()
        spec = self._parse(value)
        if spec["type"] == "crontab":
            spec = _with_timezone(spec, requested_tz or tz)
        elif requested_tz:
            raise ValueError("Intervals are elapsed durations; use a crontab for a timezone.")
        return validate_spec(spec)[0]

    def _parse(self, value: str) -> dict:
        if value in ("every second", "every minute", "every hour", "every day"):
            return {"type": "interval", "seconds": _INTERVAL_UNITS[value.split()[1]]}
        interval = re.fullmatch(r"every (\d+) (second|minute|hour|day)s?", value)
        if interval:
            return {"type": "interval", "seconds": int(interval[1]) * _INTERVAL_UNITS[interval[2]]}

        windowed = re.fullmatch(
            rf"every (\d+) (minute|hour)s? between {_TIME} and {_TIME}", value
        )
        if windowed:
            step, unit = int(windowed[1]), windowed[2]
            start_hour, start_minute = _time_parts(f"{windowed[3]}:{windowed[4]}")
            end_hour, end_minute = _time_parts(f"{windowed[5]}:{windowed[6]}")
            if step <= 0:
                raise ValueError("The interval must be positive.")
            if start_hour > end_hour or (start_hour == end_hour and start_minute > end_minute):
                raise ValueError("The end of the window must be after its start.")
            if unit == "hour":
                if start_minute != end_minute:
                    raise ValueError("Hourly windows must start and end at the same minute.")
                field = {"minute": str(start_minute),
                         "hour": ",".join(str(h) for h in range(start_hour, end_hour + 1, step))}
            else:
                # A rectangular cron cannot encode a partial final hour.
                # Refuse rather than run beyond the operator's endpoint.
                if start_hour != end_hour:
                    raise ValueError("Minute windows must fit within one hour; create separate schedules for each hour.")
                field = {"minute": ",".join(str(m) for m in range(start_minute, end_minute + 1, step)),
                         "hour": str(start_hour)}
            return {"type": "crontab", **field}

        daily = re.fullmatch(rf"(daily|weekdays|weekends) at {_TIME}", value)
        if daily:
            hour, minute = _time_parts(f"{daily[2]}:{daily[3]}")
            return {"type": "crontab", "minute": str(minute), "hour": str(hour),
                    "day_of_week": {"daily": "*", "weekdays": "1-5", "weekends": "0,6"}[daily[1]]}

        several_times = re.fullmatch(
            rf"(?:daily |twice a day )?at (.+?) on ({_DAYS})", value
        )
        if several_times:
            times = _split_times(several_times[1])
            return {"type": "crontab", "minute": str(times[0][1]),
                    "hour": _hours(times), "day_of_week": _day_field(several_times[2])}

        twice = re.fullmatch(r"(?:twice a day at|daily at) (.+)", value)
        if twice:
            times = _split_times(twice[1])
            return {"type": "crontab", "minute": str(times[0][1]),
                    "hour": _hours(times), "day_of_week": "*"}

        days_at = re.fullmatch(rf"({_DAYS}) at (.+)", value)
        if days_at:
            times = _split_times(days_at[2])
            return {"type": "crontab", "minute": str(times[0][1]),
                    "hour": _hours(times), "day_of_week": _day_field(days_at[1])}

        at_on = re.fullmatch(rf"at {_TIME} on ({_DAYS})", value)
        if at_on:
            hour, minute = _time_parts(f"{at_on[1]}:{at_on[2]}")
            return {"type": "crontab", "minute": str(minute), "hour": str(hour),
                    "day_of_week": _day_field(at_on[3])}

        fields = value.split()
        if len(fields) == 5:
            minute, hour, dom, month, dow = fields
            return {"type": "crontab", "minute": minute, "hour": hour,
                    "day_of_month": dom, "month_of_year": month, "day_of_week": dow}

        raise ValueError(
            "I could not read that schedule. Try 'every 15 minutes', "
            "'weekdays at 09:00', 'Mon and Thu at 06:30', "
            "'daily at 09:00 and 17:00', or five-field cron."
        )


class LLMParser:
    """Host-supplied AI completion, followed by the same strict validation.

    provider.complete(system, user) returns a JSON schedule spec. No vendor
    SDK or credentials are required by celery-manager. Preview is still the
    operator's confirmation of meaning; syntax validation cannot prove intent.
    """
    def __init__(self, provider):
        self.provider = provider
        self.rules = RuleBasedParser()

    def parse(self, text: str, tz: str = "UTC") -> dict:
        import json
        try:
            return self.rules.parse(text, tz)
        except ValueError:
            pass
        response = self.provider.complete(
            'Return only JSON: {"type":"crontab","minute":"...","hour":"...",'
            '"day_of_week":"...","day_of_month":"...","month_of_year":"..."} '
            'or {"type":"interval","seconds":N}. Refuse requests needing holidays, '
            'dependencies, multiple expressions, or ambiguous times with {"error":"reason"}. '
            f'Timezone: {tz}. Never invent fallback schedules.', text)
        try:
            data = json.loads(response)
        except (ValueError, TypeError) as exc:
            raise ValueError("The AI provider did not return a valid JSON schedule. Nothing was saved.") from exc
        if isinstance(data, dict) and data.get("error"):
            raise ValueError(str(data["error"]))
        return validate_spec(data)[0]


__all__ = ["LLMParser", "RuleBasedParser", "describe_spec"]
