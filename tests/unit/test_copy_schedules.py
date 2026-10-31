"""Copying definitions between scheduler backends (RedBeat/code -> SQL)."""
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from celery import Celery
from celery.schedules import crontab, schedule

pytest.importorskip("sqlalchemy_celery_beat")

from celery_manager.cli import copy_schedules_command
from celery_manager.schedulers.base import ScheduleEntry, StaticBeatBackend
from celery_manager.schedulers.reconcile import copy_schedules
from celery_manager.schedulers.sqlalchemy import SQLAlchemyBeatBackend


@pytest.fixture
def app(tmp_path):
    app = Celery("copy-contract", broker="memory://")
    app.conf.update(timezone="UTC", beat_dburi=f"sqlite:///{tmp_path / 'beat.db'}")
    app.conf.beat_schedule = {
        "nightly-export": {"task": "exports.nightly", "schedule": crontab(minute="0", hour="2"), "args": [1]},
        "cache-refresh": {"task": "cache.refresh", "schedule": timedelta(minutes=5), "options": {"queue": "fast"}},
    }
    SQLAlchemyBeatBackend(app, create_tables=True).close()
    return app


def sql(app):
    return SQLAlchemyBeatBackend(app)


def test_dry_run_plans_creates_and_writes_nothing(app):
    target = sql(app)
    try:
        result = copy_schedules(StaticBeatBackend(app), target, app=app)
        assert result["dry_run"] is True and result["applied"] == []
        assert {(c["name"], c["action"], c["apply"]) for c in result["plan"]["changes"]} == {
            ("nightly-export", "create", True), ("cache-refresh", "create", True)}
        assert list(target.list()) == []
    finally:
        target.close()


def test_apply_copies_definitions_and_keeps_target_only_entries(app):
    target = sql(app)
    try:
        target.create(ScheduleEntry("sql-only", "misc.task", schedule(60), source=target.name))
        result = copy_schedules(StaticBeatBackend(app), target, app=app, apply=True)
        assert sorted(result["applied"]) == ["cache-refresh", "nightly-export"]
        stored = {entry.name: entry for entry in target.list()}
        assert set(stored) == {"cache-refresh", "nightly-export", "sql-only"}
        assert stored["nightly-export"].args == [1]
        assert stored["cache-refresh"].options["queue"] == "fast"
        extra = [c for c in result["plan"]["changes"] if c["name"] == "sql-only"]
        assert extra[0]["action"] == "extra" and extra[0]["apply"] is False
    finally:
        target.close()


def test_differing_entries_need_allow_updates(app):
    target = sql(app)
    try:
        target.create(ScheduleEntry("nightly-export", "exports.nightly", schedule(30), source=target.name))
        result = copy_schedules(StaticBeatBackend(app), target, app=app, apply=True)
        assert result["applied"] == ["cache-refresh"]
        result = copy_schedules(StaticBeatBackend(app), target, app=app, apply=True, allow_updates=True)
        assert result["applied"] == ["nightly-export"]
        assert target.get("nightly-export").args == [1]
    finally:
        target.close()


def test_redbeat_entries_copy_into_sql(app):
    pytest.importorskip("redbeat")
    fakeredis = pytest.importorskip("fakeredis")
    from celery_manager.schedulers.redbeat import RedBeatBackend

    redis = fakeredis.FakeRedis()
    with patch("redbeat.schedulers.get_redis", return_value=redis):
        source = RedBeatBackend(app)
        source.create(ScheduleEntry("redis-owned", "reports.daily", crontab(minute="30", hour="6"), [], {"kind": "daily"},
                                    {}, source=source.name))
        target = sql(app)
        try:
            result = copy_schedules(source, target, app=app, apply=True)
            assert "redis-owned" in result["applied"]
            assert target.get("redis-owned").kwargs == {"kind": "daily"}
        finally:
            target.close()


def test_cli_prints_plan_and_applies_only_with_flag(app, capsys):
    assert copy_schedules_command(app, "code", "sql") == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["dry_run"] is True and "Dry run: 2 change(s)" in out.err
    target = sql(app)
    try:
        assert list(target.list()) == []
    finally:
        target.close()
    assert copy_schedules_command(app, "code", "sql", apply=True) == 0
    assert "Applied 2 change(s)." in capsys.readouterr().err
    target = sql(app)
    try:
        assert {entry.name for entry in target.list()} == {"cache-refresh", "nightly-export"}
    finally:
        target.close()


def test_cli_rejects_same_source_and_target(app, capsys):
    assert copy_schedules_command(app, "sql", "sql") == 2
    assert "must differ" in capsys.readouterr().err
