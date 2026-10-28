"""Deterministic English schedule parsing; ambiguous text is refused."""
from __future__ import annotations

import re

from celery_manager.schedule_specs import validate_spec


class RuleBasedParser:
    def parse(self, text: str, tz: str = "UTC") -> dict:
        value = text.strip().lower()
        interval = re.fullmatch(r"every (\d+) (second|minute|hour|day)s?", value)
        if interval:
            units = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
            spec = {"type": "interval", "seconds": int(interval[1]) * units[interval[2]]}
        else:
            daily = re.fullmatch(r"(daily|weekdays|weekends) at (\d{1,2}):(\d{2})", value)
            if daily:
                spec = {"type": "crontab", "hour": daily[2], "minute": daily[3],
                        "day_of_week": {"daily": "*", "weekdays": "1-5", "weekends": "0,6"}[daily[1]]}
            elif len(value.split()) == 5:
                minute, hour, dom, month, dow = value.split()
                spec = {"type": "crontab", "minute": minute, "hour": hour,
                        "day_of_month": dom, "month_of_year": month, "day_of_week": dow}
            else:
                raise ValueError("Use five-field cron, 'every N minutes/hours', or 'weekdays at HH:MM'.")
        return validate_spec(spec)[0]


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
