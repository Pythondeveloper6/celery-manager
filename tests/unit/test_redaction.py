"""Redaction of arguments, results, exceptions and tracebacks at every writer."""
import json
from unittest.mock import patch

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager import CeleryManagerConfig, install_publish_hook, track
from celery_manager.api.app import create_app
from celery_manager.auth.external import ExternalUser
from celery_manager.control.messages import peek_messages
from celery_manager.db.models import AuditLog, TaskRun
from celery_manager.events.listener import upsert_task_run
from celery_manager.mcp.server import McpServer
from celery_manager.redaction import scrub_text

SECRET = "s3cr3t-value-123"
BEARER = f"Bearer {SECRET}"


def make(role="admin", **extra):
    app = Celery("redaction", broker="memory://", backend="cache+memory://")

    @app.task(name="redaction.fail", bind=True)
    @track()
    def fail(self, user_id, api_token=None, payload=None):
        raise RuntimeError(f"upstream said 401 for header Authorization: {BEARER}")

    @app.task(name="redaction.ok", bind=True)
    @track()
    def ok(self, *args, **kwargs):
        return {"echo": 1}

    @app.task(name="redaction.hidden", bind=True)
    @track(redact_args=True, redact_kwargs=("payload.email",))
    def hidden(self, card, payload=None, note=""):
        return "done"

    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s",
                                 authenticate_request=lambda r: ExternalUser("u", role, "sub"), **extra)
    api, sessions = create_app(config)
    app.set_current()
    return app, config, sessions, TestClient(api, base_url="https://testserver"), fail, hidden


def dump(sessions):
    with sessions() as session:
        rows = [{c.name: getattr(r, c.name) for c in TaskRun.__table__.columns} for r in session.query(TaskRun)]
        audit = [a.details_json if hasattr(a, "details_json") else str(a.__dict__) for a in session.query(AuditLog)]
    return json.dumps(rows, default=str) + json.dumps(audit, default=str)


def test_decorator_stores_neither_secret_key_value_nor_bearer_header():
    _, _, sessions, client, fail, _ = make()
    fail.apply(task_id="t1", args=[7], kwargs={"api_token": SECRET})
    assert SECRET not in dump(sessions)
    body = client.get("/api/tasks/t1").json()
    assert json.loads(body["kwargs"]) == {"api_token": "[redacted]"}
    assert body["args_redacted"] is True and "[redacted]" in body["exception"] and SECRET not in json.dumps(body)


def test_per_task_options_hide_positional_args_and_nested_paths():
    _, _, sessions, client, _, hidden = make()
    hidden.apply(task_id="t2", args=["4111-1111"], kwargs={"payload": {"email": "a@b.c", "keep": "yes"}})
    body = client.get("/api/tasks/t2").json()
    assert json.loads(body["args"]) == ["[redacted]"]
    assert json.loads(body["kwargs"])["payload"] == {"email": "[redacted]", "keep": "yes"}
    assert "4111" not in dump(sessions) and "a@b.c" not in dump(sessions)


def test_sensitive_keys_can_be_replaced_or_disabled():
    _, _, _sessions, client, fail, _ = make(sensitive_keys=())
    fail.apply(task_id="t3", args=[1], kwargs={"api_token": "plain"})
    assert json.loads(client.get("/api/tasks/t3").json()["kwargs"]) == {"api_token": "plain"}
    with pytest.raises(ValueError):
        CeleryManagerConfig(celery_app=Celery("x"), sensitive_keys=("(",))


def test_payload_redactor_runs_last_and_a_failing_one_withholds():
    seen = []

    def hook(kind, task_name, value):
        seen.append(kind)
        return value.replace("boom", "***") if isinstance(value, str) else value

    _app, config, _sessions, client, fail, _ = make(payload_redactor=hook)
    fail.apply(task_id="t4", args=[1])
    assert {"args", "kwargs", "exception", "traceback"} <= set(seen)
    config.payload_redactor = lambda *a: 1 / 0
    fail.apply(task_id="t5", args=[1], kwargs={"payload": "abc"})
    body = client.get("/api/tasks/t5").json()
    assert "withheld" in body["exception"] and "abc" not in body["kwargs"] and body["args_redacted"]


def test_listener_event_is_redacted():
    _, config, sessions, client, _, _ = make()
    with sessions() as session:
        upsert_task_run(session, {"type": "task-received", "uuid": "ev1", "name": "x.y",
                                  "args": "('a',)", "kwargs": f"{{'api_token': '{SECRET}'}}"}, config)
        upsert_task_run(session, {"type": "task-failed", "uuid": "ev1", "exception": f"HTTP 401 {BEARER}",
                                  "traceback": f"Traceback\nValueError: token={SECRET}"}, config)
        session.commit()
    assert SECRET not in dump(sessions)
    assert client.get("/api/tasks/ev1").json()["args_redacted"] is True


def test_publish_hook_redacts_seeded_row():
    app, _, sessions, _, _, _ = make()
    uninstall = install_publish_hook(app, session_factory=sessions)
    try:
        app.send_task("redaction.ok", args=[1], kwargs={"password": SECRET}, task_id="pub1")
    finally:
        uninstall()
    assert SECRET not in dump(sessions)
    with sessions() as session:
        assert session.query(TaskRun).filter_by(task_id="pub1").one().args_redacted is True


def test_start_task_seed_and_audit_are_redacted_and_retry_needs_explicit_args():
    app, _, sessions, client, _, _ = make()
    with patch.object(app, "send_task", wraps=app.send_task):
        response = client.post("/api/tasks/start", json={"task_name": "redaction.ok", "kwargs": {"api_token": SECRET}})
    assert response.status_code == 200, response.text
    new_id = response.json()["task_id"] if "task_id" in response.json() else response.json()["id"]
    assert SECRET not in dump(sessions)
    with patch.object(app, "send_task") as send:
        assert client.post(f"/api/tasks/{new_id}/retry").status_code == 409
        assert client.post("/api/tasks/retry-bulk", json={"task_ids": [new_id]}).status_code == 409
        send.assert_not_called()
    with sessions() as session:
        session.query(TaskRun).filter_by(task_id=new_id).update({"status": "failed"})
        session.commit()
    edited = client.post(f"/api/tasks/{new_id}/retry", json={"args": [], "kwargs": {"api_token": "new-secret-99"}})
    assert edited.status_code == 200, edited.text
    assert "new-secret-99" not in dump(sessions)


def test_roles_below_view_roles_get_masked_shape_in_detail_flower_dead_letters_and_export():
    _, _, _sessions, client, fail, _ = make(role="viewer", args_view_role="operator", traceback_view_role="admin")
    fail.apply(task_id="v1", args=["visible-arg"], kwargs={"payload": "visible-kwarg"})
    body = client.get("/api/tasks/v1").json()
    assert json.loads(body["args"]) == ["str"] and json.loads(body["kwargs"]) == {"payload": "str"}
    assert body["traceback"] is None and body["traceback_restricted"] and body["args_restricted"]
    flower = client.get("/api/flower/task/info/v1").json()
    assert flower["args"] == '["str"]' and flower["traceback"] is None
    dead = client.get("/api/dead-letters").json()["items"][0]
    assert json.loads(dead["args"]) == ["str"] and dead["last_line"] is None
    export = client.get("/api/tasks/export?format=csv").text
    assert "visible-arg" not in export and "visible-kwarg" not in export and SECRET not in export
    assert "upstream said 401" in export  # exception text stays (already scrubbed at write time)


def test_higher_role_sees_stored_values():
    _, _, _, client, fail, _ = make(role="admin", args_view_role="operator", traceback_view_role="admin")
    fail.apply(task_id="v2", args=["visible-arg"])
    body = client.get("/api/tasks/v2").json()
    assert json.loads(body["args"]) == ["visible-arg"] and "Traceback" in body["traceback"]


def test_mcp_tools_follow_the_same_view_roles():
    _app, config, sessions, _, fail, _ = make()
    fail.apply(task_id="m1", args=["visible-arg"], kwargs={"api_token": SECRET})
    config.args_view_role = "operator"
    config.traceback_view_role = "operator"
    server = McpServer(config, role="viewer")
    # The MCP server builds its own app on the same config; point it at the same data.
    with sessions() as src, server.app.state.cm_session_factory() as dst:
        for run in src.query(TaskRun):
            dst.add(TaskRun(**{c.name: getattr(run, c.name) for c in TaskRun.__table__.columns if c.name != "id"}))
        dst.commit()
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "get_run", "arguments": {"task_id": "m1"}}})
    text = reply["result"]["content"][0]["text"]
    body = json.loads(text)
    assert "visible-arg" not in text and SECRET not in text
    assert json.loads(body["args"]) == ["str"] and body["traceback"] is None


def test_queue_browser_scrubs_previewed_arguments():
    app, config, sessions, _, _, _ = make()
    raw = {"transport": "redis", "supported": True, "reason": None, "approximate": False, "side_effects": False,
           "total": 1, "messages": [{"task_id": None, "task_name": "redaction.ok", "sent_at": None,
                                    "args": "('a', 'password=hunter22')", "kwargs": f"{{'api_token': '{SECRET}'}}"}]}
    with sessions() as session, patch("celery_manager.control.messages._run_with_deadline", return_value=raw):
        shown = peek_messages(app, session, "q", config=config)["messages"][0]
    assert SECRET not in json.dumps(shown) and "hunter22" not in json.dumps(shown)


def test_scrub_text_is_idempotent_and_handles_urls():
    once = scrub_text("GET https://h/x?X-Sig=abc123&signature=zzz9 Authorization: Bearer abcdef12345")
    assert "zzz9" not in once and "abcdef12345" not in once
    assert scrub_text(once) == once
