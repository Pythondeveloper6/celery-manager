from unittest.mock import MagicMock

from celery import Celery

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.schedule_health import schedule_health
from celery_manager.db.models import PeriodicSchedule


def test_consumer_checks_distinguish_absent_evidence_and_observed_missing_queue():
    app = Celery("schedule-health")
    app.conf.beat_schedule = {"job": {"task": "demo", "schedule": 60, "options": {"queue": "reports"}}}
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="test-secret")
    _, sessions = create_app(config)
    app.control.inspect = MagicMock()
    app.control.inspect.return_value.active_queues.return_value = None
    with sessions() as session:
        result = schedule_health(config, session, inspect_consumers=True)
        assert result["consumers"]["state"] == "unknown"
        assert not any(i["kind"] == "no_observed_consumer" for i in result["items"])
        app.control.inspect.return_value.active_queues.return_value = {"worker": [{"name": "celery"}]}
        result = schedule_health(config, session, inspect_consumers=True)
        assert {"name": "job", "kind": "no_observed_consumer", "queue": "reports"} in result["items"]
        session.add(PeriodicSchedule(name="removed", task_name="demo", schedule_spec="60", enabled=False))
        session.commit()
        result = schedule_health(config, session)
        assert {"name": "removed", "kind": "historical_entry_missing_from_backend"} in result["items"]
        assert result["consumers"]["state"] == "not_requested"
