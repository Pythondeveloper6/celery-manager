"""`run_late`: scheduled occurrences that started late or have not started."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from celery import Celery

from celery_manager import CeleryManagerConfig
from celery_manager.alerts.depth import check
from celery_manager.alerts.evaluator import CheckUnavailable, _summary, dashboard_link
from celery_manager.control.alerts import RULE_KINDS
from celery_manager.db.models import TaskRun
from celery_manager.db.session import get_session_factory, init_db
from tests.unit.test_alert_depth import NOW, params


@pytest.fixture
def env():
    config = CeleryManagerConfig(celery_app=Celery("late"), db_url="sqlite:///:memory:", secret_key="s")
    engine = init_db(config)
    yield config, get_session_factory(engine)
    engine.dispose()


def scheduled(task_id, *, due_ago, start_after=None, status="succeeded", name="reports.daily"):
    due = NOW - timedelta(minutes=due_ago)
    started = None if start_after is None else due + timedelta(seconds=start_after)
    return TaskRun(task_id=task_id, task_name=name, status=status, schedule_name="daily", occurrence_at=due,
                   started_at=started, finished_at=started, args="[]", kwargs="{}")


def judge(session, target="*"):
    probe = SimpleNamespace(session=session, now=NOW, config=None)
    return check(SimpleNamespace(kind="run_late", target=target), params("run_late"), probe)


def test_run_late_is_a_creatable_rule_kind():
    assert "run_late" in RULE_KINDS
    assert params("run_late") == {"seconds": 300, "window_minutes": 60}


def test_late_start_and_not_yet_started_fire_on_time_does_not(env):
    _, sessions = env
    with sessions() as session:
        session.add_all([
            scheduled("on-time", due_ago=30, start_after=20),
            scheduled("late", due_ago=30, start_after=900),
            scheduled("waiting", due_ago=20, status="queued"),
            scheduled("just-due", due_ago=2, status="queued"),
            scheduled("old", due_ago=600, start_after=900),  # outside the window
        ])
        session.commit()
        firing = judge(session).firing
    assert set(firing) == {"late", "waiting"}
    assert firing["late"]["late_seconds"] == 900 and firing["late"]["started"] is True
    assert firing["waiting"]["started"] is False and firing["waiting"]["late_seconds"] == 1200


def test_a_late_run_recovers_once_it_leaves_the_window(env):
    _, sessions = env
    with sessions() as session:
        session.add(scheduled("late", due_ago=30, start_after=900))
        session.commit()
        assert judge(session).firing
        session.query(TaskRun).update({"occurrence_at": NOW - timedelta(hours=3)})
        session.add(scheduled("fine", due_ago=10, start_after=1))
        session.commit()
        assert not judge(session).firing


def test_finished_run_without_a_start_time_is_unknown(env):
    _, sessions = env
    with sessions() as session:
        session.add(scheduled("no-start", due_ago=30, status="succeeded"))
        session.commit()
        observation = judge(session)
    assert observation.unknown == {"no-start"} and not observation.firing


def test_no_scheduled_runs_is_unknown(env):
    _, sessions = env
    with sessions() as session, pytest.raises(CheckUnavailable):
        judge(session)


def test_summary_and_links_point_at_the_run():
    rule = SimpleNamespace(kind="run_late", name="late")
    details = {"task_name": "reports.daily", "schedule": "daily", "due_at": "2026-10-29T11:30:00+00:00",
               "started": False, "late_seconds": 1200.0}
    assert _summary(rule, "t-1", details, "firing") == (
        "reports.daily (daily) due 2026-10-29T11:30:00+00:00 has not started 1200s late")
    config = SimpleNamespace(dashboard_url="https://ops.example.com/celery/")
    assert dashboard_link(config, "run_late", "t-1") == "https://ops.example.com/celery/#/tasks/t-1"
    assert dashboard_link(config, "task_failed", "a b") == "https://ops.example.com/celery/#/tasks/a%20b"
    assert dashboard_link(config, "consecutive_failures", "reports.daily") == (
        "https://ops.example.com/celery/#/task-types/reports.daily")
