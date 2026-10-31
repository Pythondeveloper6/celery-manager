"""Production adoption regressions."""
import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import inspect, select
from sqlalchemy.dialects import mssql, mysql

from celery_manager import CeleryManagerConfig, install_publish_hook, task_url, track
from celery_manager.api.app import create_app
from celery_manager.auth.external import ExternalUser
from celery_manager.auth.providers import CallableProvider
from celery_manager.cron import LLMParser, RuleBasedParser
from celery_manager.db.dialect import utcnow
from celery_manager.db.models import AuditLog, TaskRun
from celery_manager.db.session import init_db
from celery_manager.events.listener import EventListener, upsert_task_run
from celery_manager.logcapture import BoundedBuffer, capture_logs


@pytest.fixture
def env():
    app = Celery("production", broker="memory://", backend="cache+memory://")

    @app.task(name="production.allowed", shared=False)
    @track()
    def task(value=1):
        logging.getLogger("production.task").warning("token=secret %s", value)
        return {"value": value}

    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="test-secret",
                                authenticate_request=lambda r: ExternalUser("operator", "admin", "subject-42"))
    api, sessions = create_app(config)
    return app, config, sessions, TestClient(api, base_url="https://testserver"), task


def test_empty_allowlist_blocks_start_retry_bulk_and_deadletter_without_publishing(env):
    app, config, sessions, client, _ = env
    config.startable_tasks = set()
    with sessions() as session:
        session.add(TaskRun(task_id="old", task_name="production.allowed", status="failed", args="[]", kwargs="{}"))
        session.commit()
    with patch.object(app, "send_task") as send:
        assert client.post("/api/tasks/start", json={"task_name": "production.allowed"}).status_code == 403
        assert client.post("/api/tasks/old/retry").status_code == 403
        assert client.post("/api/tasks/retry-bulk", json={"task_ids": ["old"]}).status_code == 403
        assert client.post("/api/dead-letters/rerun", json={"task_ids": ["old"]}).status_code == 403
        send.assert_not_called()
    with sessions() as session:
        rows = session.query(AuditLog).filter_by(action="start_denied").all()
        assert len(rows) == 4
        assert all(r.actor_subject == "subject-42" for r in rows)
    assert client.get("/api/tasks/registered").json()["items"] == []


def test_unknown_task_and_queue_override_are_denied(env):
    app, config, _, client, _ = env
    with patch.object(app, "send_task") as send:
        assert client.post("/api/tasks/start", json={"task_name": "not.registered"}).status_code == 422
        assert client.post("/api/tasks/start", json={"task_name": "production.allowed", "queue": "other"}).status_code == 403
        send.assert_not_called()
    config.allow_queue_override = True
    with patch.object(app, "send_task", return_value=MagicMock(id="queue-ok")) as send:
        assert client.post("/api/tasks/start", json={"task_name": "production.allowed", "queue": "other"}).status_code == 200
        assert send.call_args.kwargs["queue"] == "other"


def test_argument_schema_and_callable_policy_receive_principal(env):
    app, config, _, client, _ = env
    class Arguments(BaseModel):
        value: int
    config.args_schemas = {"production.allowed": Arguments}
    principals = []
    config.startable_tasks = lambda name, principal: principals.append(principal) or principal.subject == "subject-42"
    with patch.object(app, "send_task", return_value=MagicMock(id="schema-ok")) as send:
        bad = client.post("/api/tasks/start", json={"task_name": "production.allowed", "args": ["bad"]})
        assert bad.status_code == 422
        send.assert_not_called()
        good = client.post("/api/tasks/start", json={"task_name": "production.allowed", "args": [3]})
        assert good.status_code == 200
    assert principals and principals[0].subject == "subject-42"


def test_retry_argument_editing_can_be_disabled(env):
    app, config, sessions, client, _ = env
    config.retry_edit_args = False
    with sessions() as session:
        session.add(TaskRun(task_id="retry-noedit", task_name="production.allowed", status="failed"))
        session.commit()
    with patch.object(app, "send_task", return_value=MagicMock(id="new")) as send:
        assert client.post("/api/tasks/retry-noedit/retry", json={"args": [7]}).status_code == 403
        send.assert_not_called()
        assert client.post("/api/tasks/retry-noedit/retry").status_code == 200


@pytest.mark.parametrize("limit", [0, 1, 12, 50, 256_000])
def test_ten_megabyte_capture_is_bounded_and_keeps_both_ends(limit):
    buffer = BoundedBuffer(limit)
    buffer.write("HEAD" + "é" * 5_000_000 + "TAIL")
    output = buffer.getvalue()
    assert len(output.encode()) <= limit
    assert len(buffer.head) + len(buffer.tail) <= limit
    assert buffer.truncated
    if limit >= 50:
        assert output.startswith("HEAD") and output.endswith("TAIL")


def test_log_filters_redaction_and_thread_isolation(env):
    _, config, _, _, _ = env
    config.log_capture_loggers = ("production",)
    config.log_capture_level = logging.WARNING
    config.log_redactor = lambda value: value.replace("secret", "[redacted]")
    with capture_logs(config) as buffer:
        logging.getLogger("other").warning("not captured")
        logging.getLogger("production.task").warning("secret")
        thread = threading.Thread(target=lambda: logging.getLogger("production.task").warning("other thread"))
        thread.start(); thread.join()
    assert "[redacted]" in buffer.getvalue()
    assert "secret" not in buffer.getvalue()
    assert "other thread" not in buffer.getvalue()
    assert "not captured" not in buffer.getvalue()


def test_failed_redaction_withholds_raw_logs():
    def broken(value):
        raise ValueError("unavailable")
    buffer = BoundedBuffer(100, broken)
    buffer.write("secret")
    assert "secret" not in buffer.getvalue()
    assert "withheld" in buffer.getvalue()


def test_decorator_caps_results_and_logs_without_changing_return(env):
    _, config, sessions, client, task = env
    config.log_capture_max_bytes = 80
    config.result_max_bytes = 100
    result = task.apply(args=["x" * 1000], task_id="bounded")
    assert result.get() == {"value": "x" * 1000}
    body = client.get("/api/tasks/bounded").json()
    assert body["result_truncated"] and len(body["result_repr"].encode()) <= 100
    assert body["log_output_truncated"] and len(body["log_output"].encode()) <= 80
    with sessions() as session:
        upsert_task_run(session, {"type": "task-succeeded", "uuid": "bounded", "result": "less accurate"}, config)
        session.commit()
    assert client.get("/api/tasks/bounded").json()["result_repr"] == body["result_repr"]


def test_results_opt_out_applies_to_decorator_and_listener(env):
    _, config, sessions, _, task = env
    config.store_results = False
    task.apply(task_id="private-result")
    with sessions() as session:
        upsert_task_run(session, {"type": "task-succeeded", "uuid": "private-result", "result": "sensitive"}, config)
        session.commit()
        assert session.query(TaskRun).filter_by(task_id="private-result").one().result_repr is None


def test_log_view_role_is_enforced(env):
    _, config, _, client, task = env
    config.log_view_role = "admin"
    config.authenticate_request = lambda r: ExternalUser("reader", "viewer")
    task.apply(task_id="restricted")
    body = client.get("/api/tasks/restricted").json()
    assert body["logs_restricted"] is True and body["log_output"] is None


def test_publish_hook_seeds_after_publish_and_records_initiator(env):
    app, _, sessions, client, task = env
    app.set_current()
    uninstall = install_publish_hook(app, session_factory=sessions, initiator=lambda: "person-123")
    try:
        task.apply_async(args=[2], task_id="published")
        body = client.get("/api/tasks/published").json()
        assert body["status"] == "queued" and body["initiator"] == "person-123"
        assert json.loads(body["args"]) == [2]
    finally:
        uninstall()


def test_task_link_keeps_mount_and_escapes_identifier():
    assert task_url("https://host/manager/#/old", "id/a ?") == "https://host/manager/#/tasks/id%2Fa%20%3F"


def test_provider_chain_precedence_and_subject():
    config = CeleryManagerConfig(celery_app=Celery("providers"), db_url="sqlite:///:memory:", secret_key="s",
                                local_login_enabled=False, principal_providers=(
                                    CallableProvider(lambda r: None),
                                    CallableProvider(lambda r: ExternalUser("host", "viewer", "stable-id")),
                                ))
    app, _ = create_app(config)
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/whoami").json()["subject"] == "stable-id"


def test_poll_mode_has_no_stream_route():
    config = CeleryManagerConfig(celery_app=Celery("poll"), db_url="sqlite:///:memory:", secret_key="s", live_stream="poll")
    app, _ = create_app(config)
    assert "/api/stream" not in app.openapi()["paths"]


def test_trusted_origin_and_frame_headers():
    config = CeleryManagerConfig(celery_app=Celery("cors"), db_url="sqlite:///:memory:", secret_key="s",
                                trusted_origins=("https://host.example",), frame_ancestors=("https://host.example",))
    app, _ = create_app(config)
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/auth/logout", headers={"origin": "https://evil.example"}).status_code == 403
    response = client.post("/api/auth/logout", headers={"origin": "https://host.example"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://host.example"
    assert "https://host.example" in response.headers["content-security-policy"]
    assert "x-frame-options" not in response.headers


def test_bridge_requires_fresh_auth_and_cannot_extend_itself(env):
    _, config, _, client, _ = env
    config.session_bridge_enabled = True
    config.local_login_enabled = False
    assert client.post("/api/auth/bridge").status_code == 200
    config.authenticate_request = lambda r: None
    assert client.get("/api/whoami").status_code == 200
    assert client.post("/api/auth/bridge").status_code == 401


def test_event_listener_enables_events_and_can_opt_out(env):
    app, config, sessions, _, _ = env
    with patch.object(app.control, "enable_events") as enable, patch.object(app, "connection"), patch.object(app.events, "Receiver"):
        EventListener(config, sessions).run()
        enable.assert_called_once()
        enable.reset_mock()
        EventListener(config, sessions).run(enable_events=False)
        enable.assert_not_called()


def test_utc_defaults_compile_per_dialect():
    assert "SYSUTCDATETIME()" in str(select(utcnow()).compile(dialect=mssql.dialect()))
    assert "UTC_TIMESTAMP()" in str(select(utcnow()).compile(dialect=mysql.dialect()))


def test_sqlite_main_schema_and_engine_options():
    config = CeleryManagerConfig(celery_app=None, db_url="sqlite:///:memory:", db_schema="main",
                                engine_options={"pool_pre_ping": True})
    engine = init_db(config)
    assert "task_runs" in inspect(engine).get_table_names(schema="main")
    engine.dispose()


@pytest.mark.parametrize("text, expected", [("every 15 minutes", {"type": "interval", "seconds": 900}),
                                            ("weekdays at 06:30", {"hour": "6", "minute": "30", "day_of_week": "1-5"})])
def test_rule_parser(text, expected):
    parsed = RuleBasedParser().parse(text)
    assert all(str(parsed[key]).lstrip("0") == str(value).lstrip("0") for key, value in expected.items())


def test_ambiguous_and_ai_output_are_validated():
    with pytest.raises(ValueError):
        RuleBasedParser().parse("after work except holidays")
    provider = MagicMock()
    provider.complete.return_value = '{"type":"crontab","hour":"99"}'
    with pytest.raises(ValueError):
        LLMParser(provider).parse("each evening")
    provider.complete.return_value = '{"type":"interval","seconds":600}'
    assert LLMParser(provider).parse("each ten minutes")["seconds"] == 600


def test_cancel_unknown_id_requires_explicit_admin_force(env):
    app, config, _, client, _ = env
    with patch.object(app.control, "revoke") as revoke:
        assert client.post("/api/tasks/unknown/cancel").status_code == 404
        revoke.assert_not_called()
        config.authenticate_request = lambda r: ExternalUser("op", "operator")
        assert client.post("/api/tasks/unknown/cancel?force=true").status_code == 403
        config.authenticate_request = lambda r: ExternalUser("admin", "admin")
        assert client.post("/api/tasks/unknown/cancel?force=true").status_code == 200
        revoke.assert_called_once()


def test_worker_event_liveness_and_stuck_findings_do_not_change_run_status(env):
    from datetime import datetime, timedelta, timezone

    from celery_manager.control.health import attention_runs
    from celery_manager.events.workers import record_worker_event
    _, _, sessions, client, _ = env
    now = datetime.now(timezone.utc)
    with sessions() as session:
        record_worker_event(session, {"type": "worker-heartbeat", "hostname": "worker-1", "timestamp": now.timestamp()})
        record_worker_event(session, {"type": "worker-offline", "hostname": "worker-1", "timestamp": now.timestamp() + 1})
        record_worker_event(session, {"type": "worker-heartbeat", "hostname": "worker-1", "timestamp": now.timestamp() - 10})
        session.add(TaskRun(task_id="possibly-lost", task_name="production.allowed", status="started", worker="worker-1",
                            started_at=now - timedelta(minutes=5)))
        session.commit()
        assert attention_runs(session, now=now)[0]["kind"] == "worker_lost"
        assert session.query(TaskRun).one().status == "started"
    assert client.get("/api/health/workers").json()["items"][0]["state"] == "offline"
    assert client.get("/api/health/beat").json()["state"] == "unknown"


def test_teams_channel_uses_adaptive_card_and_masks_credentials(env):
    from celery_manager.alerts.senders import AlertMessage, send_teams
    from celery_manager.control.alerts import create_channel
    _, config, sessions, _, _ = env
    with sessions() as session:
        channel = create_channel(session, "host", "Teams", "teams", {"url": "https://teams.example/secret"})
        assert "secret" not in json.dumps(channel)
    with patch("celery_manager.alerts.senders.post_json") as post:
        send_teams({"url": "https://teams.example/secret"}, AlertMessage("firing", "Failed task", url="https://ops/tasks"), config)
        body = json.loads(post.call_args.args[1])
        assert body["attachments"][0]["content"]["type"] == "AdaptiveCard"


def test_new_attempt_clears_previous_output_and_truncation_flags(env):
    _, config, sessions, _, task = env
    with sessions() as session:
        session.add(TaskRun(task_id="again", task_name="production.allowed", status="failed",
                            result_repr="old result", result_truncated=True, log_output="old logs", log_output_truncated=True))
        session.commit()
    config.store_results = False
    task.apply(task_id="again")
    with sessions() as session:
        row = session.query(TaskRun).filter_by(task_id="again").one()
        assert row.result_repr is None and row.result_truncated is False
        assert "old logs" not in row.log_output
        assert row.log_output_truncated is False
