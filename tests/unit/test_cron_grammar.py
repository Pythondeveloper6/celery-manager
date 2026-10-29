"""Extended natural-language grammar, descriptions and per-entry timezones."""
from datetime import datetime, timezone

import pytest
from celery import Celery

from celery_manager.cron import RuleBasedParser
from celery_manager.occurrences import next_occurrence_after
from celery_manager.schedule_specs import (
    describe_spec,
    spec_from_schedule,
    validate_spec,
)

APP = Celery("cron-grammar")
APP.conf.timezone = "UTC"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Mon and Thu at 06:30", {"minute": "30", "hour": "6", "day_of_week": "1,4"}),
        ("Monday, Wednesday and Friday at 18:00", {"minute": "0", "hour": "18", "day_of_week": "1,3,5"}),
        ("Mon-Fri at 09:00", {"minute": "0", "hour": "9", "day_of_week": "1-5"}),
        ("Mon to Fri at 09:00", {"minute": "0", "hour": "9", "day_of_week": "1-5"}),
        ("at 06:30 on Mon and Thu", {"minute": "30", "hour": "6", "day_of_week": "1,4"}),
        ("daily at 09:00 and 17:00", {"minute": "0", "hour": "9,17", "day_of_week": "*"}),
        ("twice a day at 09:00 and 17:00", {"minute": "0", "hour": "9,17", "day_of_week": "*"}),
        ("weekdays at 09:00", {"minute": "0", "hour": "9", "day_of_week": "1-5"}),
        ("weekends at 10:15", {"minute": "15", "hour": "10", "day_of_week": "0,6"}),
    ],
)
def test_weekday_and_multiple_time_phrases(text, expected):
    spec = RuleBasedParser().parse(text)
    assert all(spec[key] == value for key, value in expected.items())


def test_windowed_interval_preserves_bounds_and_step():
    spec = RuleBasedParser().parse("every 15 minutes between 09:10 and 09:40")
    assert spec["minute"] == "10,25,40"
    assert spec["hour"] == "9"
    spec = RuleBasedParser().parse("every 2 hours between 09:30 and 17:30")
    assert spec["hour"] == "9,11,13,15,17"
    assert spec["minute"] == "30"


@pytest.mark.parametrize("phrase", [
    "every 15 minutes between 09:00 and 17:00",
    "every 2 hours between 09:30 and 17:00",
    "every 0 hours between 09:00 and 17:00",
    "every 5 minutes between 29:00 and 29:30",
    "daily at 25:00", "at 12:70 on Mon",
])
def test_windows_and_times_never_silently_change_meaning(phrase):
    with pytest.raises(ValueError):
        RuleBasedParser().parse(phrase)


@pytest.mark.parametrize(
    "text",
    [
        "daily at 09:00 and 09:30",  # two different minutes: two schedules
        "Mon at 06:30 and Thu at 07:00 on Fridays",
        "every 15 minutes between 17:00 and 09:00",  # backwards window
        "Mon and Funday at 09:00",
        "every 0 minutes",
    ],
)
def test_ambiguous_or_impossible_text_is_refused(text):
    with pytest.raises(ValueError):
        RuleBasedParser().parse(text)


def test_descriptions_are_deterministic_sentences():
    assert describe_spec(RuleBasedParser().parse("every 15 minutes")) == "Every 15 minutes"
    assert describe_spec(RuleBasedParser().parse("every 2 hours")) == "Every 2 hours"
    assert "Monday" in describe_spec(RuleBasedParser().parse("Mon and Thu at 06:30"))
    assert "Thursday" in describe_spec(RuleBasedParser().parse("Mon and Thu at 06:30"))


def test_per_entry_timezone_changes_the_projected_utc_time():
    spec = {"type": "crontab", "minute": "30", "hour": "6", "day_of_week": "*",
            "day_of_month": "*", "month_of_year": "*", "timezone": "Europe/Berlin"}
    _, schedule = validate_spec(spec, app=APP)
    at = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
    # 06:30 Berlin (CET, UTC+1) is 05:30 UTC.
    assert next_occurrence_after(schedule, at) == datetime(2026, 10, 29, 5, 30, tzinfo=timezone.utc)
    assert spec_from_schedule(schedule)["timezone"] == "Europe/Berlin"


def test_unknown_timezone_is_refused():
    with pytest.raises(ValueError):
        RuleBasedParser().parse("Mon at 06:30 in Mars/Olympus")
    with pytest.raises(ValueError):
        validate_spec({"type": "crontab", "timezone": "Mars/Olympus"})


def test_trailing_timezone_is_kept_on_the_spec():
    assert RuleBasedParser().parse("Mon at 06:30 in Europe/Berlin")["timezone"] == "Europe/Berlin"


def test_timezone_schedule_becomes_due_and_survives_pickle():
    import pickle
    spec = RuleBasedParser().parse("daily at 06:30 in Europe/Berlin")
    _, schedule = validate_spec(spec, app=APP)
    schedule = pickle.loads(pickle.dumps(schedule))
    schedule.app = APP
    last = datetime(2026, 10, 28, 5, 30, tzinfo=timezone.utc)
    schedule.nowfun = lambda: datetime(2026, 10, 29, 5, 29, tzinfo=timezone.utc)
    assert schedule.is_due(last).is_due is False
    assert schedule.is_due(last).next == 60
    schedule.nowfun = lambda: datetime(2026, 10, 29, 5, 30, tzinfo=timezone.utc)
    assert schedule.is_due(last).is_due is True
    assert spec_from_schedule(schedule)["timezone"] == "Europe/Berlin"


@pytest.mark.parametrize("at, expected", [
    (datetime(2026, 3, 28, 6, tzinfo=timezone.utc), datetime(2026, 3, 29, 4, 30, tzinfo=timezone.utc)),
    (datetime(2026, 10, 24, 6, tzinfo=timezone.utc), datetime(2026, 10, 25, 5, 30, tzinfo=timezone.utc)),
])
def test_timezone_calendar_crosses_dst_using_elapsed_utc_time(at, expected):
    _, schedule = validate_spec(RuleBasedParser().parse("daily at 06:30 in Europe/Berlin"), app=APP)
    assert next_occurrence_after(schedule, at) == expected
