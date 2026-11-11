"""The MCP server: protocol shape, role-gated tool lists, argument validation,
and real calls through the dashboard's own routes (roles, audit, dry runs)."""
import io
import json
from datetime import datetime, timezone

import pytest
from celery import Celery

from celery_manager import CeleryManagerConfig
from celery_manager.auth.external import ExternalUser
from celery_manager.cli import build_parser
from celery_manager.db.models import AuditLog, TaskRun
from celery_manager.mcp.server import McpServer, serve
from celery_manager.mcp.tools import TOOLS, allowed


def make(role="viewer", **extra):
    app = Celery("mcp_test", broker="memory://", backend="cache+memory://")

    @app.task(name="demo.work")
    def work():
        return None

    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s", **extra)
    server = McpServer(config, role=role, agent_name="triage-bot")
    with server.app.state.cm_session_factory() as session:
        for i, status in enumerate(["failed", "failed", "succeeded"]):
            session.add(TaskRun(task_id=f"r{i}", task_name="demo.work", status=status,
                                started_at=datetime(2026, 11, 11, 10, tzinfo=timezone.utc),
                                exception="ValueError: boom" if status == "failed" else None, traceback="tb"))
        session.commit()
    return server


def rpc(server, method, params=None, ident=1):
    return server.handle({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})


def call(server, name, arguments=None):
    reply = rpc(server, "tools/call", {"name": name, "arguments": arguments or {}})
    result = reply["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def test_initialize_and_unknown_method():
    server = make()
    reply = rpc(server, "initialize", {"protocolVersion": "2024-11-05"})["result"]
    assert reply["protocolVersion"] == "2024-11-05" and reply["serverInfo"]["name"] == "celery-manager"
    assert "untrusted" in reply["instructions"]
    assert rpc(server, "initialize", {"protocolVersion": "1999"})["result"]["protocolVersion"] == "2025-06-18"
    assert rpc(server, "bogus")["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"id": 1})["error"]["code"] == -32600


def test_tool_list_follows_the_role():
    names = {r: {t["name"] for t in rpc(make(r), "tools/list")["result"]["tools"]} for r in ("viewer", "operator", "admin")}
    assert "list_runs" in names["viewer"] and "retry_run" not in names["viewer"] and "purge_queue" not in names["operator"]
    assert names["viewer"] < names["operator"] < names["admin"]
    assert {t.name for t in allowed("admin")} == {t.name for t in TOOLS}
    for tool in rpc(make("admin"), "tools/list")["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object" and tool["description"]
        writes = tool["name"] not in names["viewer"] and tool["name"] != "run_doctor"
        assert tool["annotations"]["readOnlyHint"] is not writes


def test_hidden_tools_cannot_be_called():
    error, body = call(make("viewer"), "retry_run", {"task_id": "r0"})
    assert error and "unknown tool" in body["error"]


def test_reads_go_through_the_real_routes():
    server = make()
    error, body = call(server, "list_runs", {"status": "failed"})
    assert not error and body["total"] == 2
    error, body = call(server, "get_run", {"task_id": "r0"})
    assert not error and body["exception"] == "ValueError: boom"
    error, body = call(server, "failure_groups", {"range": "30d"})
    assert not error
    error, body = call(server, "get_run", {"task_id": "nope"})
    assert error and body["error"] == "HTTP 404"
    error, body = call(server, "whoami")
    assert body["username"] == "mcp:triage-bot" and body["role"] == "viewer"
    assert not call(server, "llm_usage")[0]


def test_argument_validation_names_the_field():
    server = make()
    assert "missing required" in call(server, "get_run", {})[1]["error"]
    assert "unknown argument" in call(server, "list_runs", {"bogus": 1})[1]["error"]
    assert "limit must be between" in call(server, "list_runs", {"limit": 5000})[1]["error"]
    assert "must be integer" in call(server, "list_runs", {"limit": "5"})[1]["error"]
    assert "must be integer" in call(server, "list_runs", {"limit": True})[1]["error"]
    assert "one of" in call(server, "failure_groups", {"range": "1y"})[1]["error"]
    assert call(server, "list_runs", None)[0] is False


def test_path_values_cannot_escape_their_segment():
    for bad in ("../workers", "a/b", "a\\b", "a?b", "a#b", "a\nb", ""):
        error, body = call(make(), "get_run", {"task_id": bad})
        assert error and ("must not contain" in body["error"] or "missing required" in body["error"])


def test_writes_are_audited_as_the_agent():
    server = make("operator")
    error, body = call(server, "cancel_run", {"task_id": "r0", "reason": "stuck"})
    # The in-memory broker cannot revoke, but the route must have run as the agent.
    with server.app.state.cm_session_factory() as session:
        users = {row.username for row in session.query(AuditLog)}
    assert users <= {"mcp:triage-bot"}
    assert error is False or body.get("error", "").startswith("HTTP")


def test_bulk_retry_defaults_to_a_dry_run():
    server = make("operator")
    error, body = call(server, "retry_runs", {"task_ids": ["r0", "r1"]})
    assert not error
    with server.app.state.cm_session_factory() as session:
        assert session.query(TaskRun).count() == 3  # nothing dispatched or recorded
    error, body = call(server, "retry_runs", {"task_ids": ["r0"] * 501})
    assert error and "more than 500" in body["error"]


def test_purge_needs_admin_and_a_matching_confirmation():
    assert "purge_queue" not in {t["name"] for t in rpc(make("operator"), "tools/list")["result"]["tools"]}
    server = make("admin", require_typed_confirmation=True)
    error, body = call(server, "purge_queue", {"name": "celery", "confirm": "other"})
    assert error and body["error"] == "HTTP 400"
    assert call(server, "purge_queue", {"name": "celery"})[1]["error"].startswith("missing")


def test_doctor_runs_directly():
    error, body = call(make("admin"), "run_doctor")
    assert not error and "checks" in body


def test_large_results_are_truncated(monkeypatch):
    import celery_manager.mcp.server as module
    monkeypatch.setattr(module, "MAX_RESULT_CHARS", 50)
    error, body = call(make(), "list_runs")
    assert not error and body["truncated"] is True


def test_stdio_loop_survives_garbage():
    server = make()
    lines = ["not json", "", json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}),
             json.dumps([{"jsonrpc": "2.0", "id": 8, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}])]
    out = io.StringIO()
    serve(server, io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert replies[2] == [{"jsonrpc": "2.0", "id": 8, "result": {}}]


def test_cli_registers_the_command_and_rejects_bad_roles():
    parser = build_parser()
    args = parser.parse_args(["mcp", "--app", "a:b", "--db-url", "sqlite://", "--role", "operator"])
    assert args.command == "mcp" and args.role == "operator"
    with pytest.raises(SystemExit):
        parser.parse_args(["mcp", "--role", "root"])


def test_real_bulk_retry_needs_a_matching_confirm_count():
    server = make("operator")
    for extra in ({}, {"confirm_count": 1}):
        error, body = call(server, "retry_runs", {"task_ids": ["r0", "r1"], "dry_run": False, **extra})
        assert error and "confirm_count" in body["error"]
    with server.app.state.cm_session_factory() as session:
        assert session.query(TaskRun).count() == 3


def test_environment_header_is_percent_encoded_and_accepted():
    server = make("operator", operator_safeguards=True, environment_name="Produktion\u2013EU 100%")
    error, body = call(server, "cancel_run", {"task_id": "r0", "reason": "stuck"})
    assert not (error and body.get("error") == "HTTP 428"), body
    from celery_manager.mcp.client import _header_value
    assert _header_value("Produktion\u2013EU") == b"Produktion%E2%80%93EU"
    assert _header_value("a%20b") == b"a%20b"


def test_doctor_is_admin_only_and_uses_the_original_config():
    assert "run_doctor" not in {t["name"] for t in rpc(make("operator"), "tools/list")["result"]["tools"]}
    assert call(make("operator"), "run_doctor")[1]["error"].startswith("unknown tool")
    server = make("admin")
    assert server.original_config is not server.config and server.original_config.authenticate_request is None
    seen = {}
    import celery_manager.doctor as doctor_module
    original = doctor_module.doctor
    doctor_module.doctor = lambda config, **kw: seen.setdefault("config", config) and {"checks": []}
    try:
        call(server, "run_doctor")
    finally:
        doctor_module.doctor = original
    assert seen["config"] is server.original_config


def test_path_arguments_with_spaces_route_correctly():
    server = make()
    with server.app.state.cm_session_factory() as session:
        session.add(TaskRun(task_id="id with space", task_name="demo.work", status="failed",
                            started_at=datetime(2026, 11, 11, 10, tzinfo=timezone.utc)))
        session.commit()
    error, body = call(server, "get_run", {"task_id": "id with space"})
    assert not error and body["task_id"] == "id with space"


def test_client_gives_asgi_the_decoded_path_and_raw_bytes():
    from celery_manager.mcp.client import InProcessClient
    seen = {}

    async def app(scope, receive, send):
        seen.update(path=scope["path"], raw=scope["raw_path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    InProcessClient(app).request("GET", "/api/tasks/id%20with%20space")
    assert seen == {"path": "/api/tasks/id with space", "raw": b"/api/tasks/id%20with%20space"}


def test_purge_preview_and_every_write_accept_a_reason():
    for tool in TOOLS:
        if tool.writes:
            assert "reason" in tool.schema["properties"], tool.name
    server = make("admin", operator_safeguards=True, environment_name="prod")
    _error, body = call(server, "preview_queue_purge", {"name": "celery", "reason": "checking depth"})
    assert body.get("error") != "HTTP 428", body


def test_destructive_hints():
    tools = {t["name"]: t["annotations"] for t in rpc(make("admin"), "tools/list")["result"]["tools"]}
    for name in ("cancel_run", "start_task", "retry_run", "retry_runs", "pause_schedule", "resume_schedule",
                 "set_rate_limit", "acknowledge_dead_letter", "purge_queue"):
        assert tools[name] == {"readOnlyHint": False, "destructiveHint": True}, name
    assert tools["preview_queue_purge"] == {"readOnlyHint": False, "destructiveHint": False}
    assert tools["list_runs"]["destructiveHint"] is False


def test_principal_providers_are_ignored():
    server = make("viewer", principal_providers=(lambda request: ExternalUser("intruder", "admin", subject="x"),))
    assert server.config.principal_providers == ()
    assert call(server, "whoami")[1]["username"] == "mcp:triage-bot"


def test_non_finite_and_out_of_range_numbers_are_rejected():
    server = make("operator")
    assert "must be between" in call(server, "list_runs", {"offset": 2_000_000})[1]["error"]
    assert "must be between" in call(server, "start_task", {"task_name": "demo.work", "countdown": 10**9})[1]["error"]
    error, body = call(server, "start_task", {"task_name": "demo.work", "countdown": float("inf")})
    assert error and "must be" in body["error"]
    error, body = call(server, "start_task", {"task_name": "demo.work", "args": [float("nan")]})
    assert error and "finite" in body["error"]


def _serve(server, text):
    out = io.StringIO()
    serve(server, io.StringIO(text), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_stdio_hardening():
    server = make()
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    replies = _serve(server, "[" * 200_000 + "\n" + '{"jsonrpc":"2.0","id":2,"method":"ping","x":NaN}\n'
                     + "[]\n" + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": [1]}) + "\n"
                     + "x" * ((1 << 20) + 5) + "\n" + ping + "\n")
    assert [r["error"]["code"] for r in replies[:5]] == [-32700, -32700, -32600, -32602, -32600]
    assert replies[5] == {"jsonrpc": "2.0", "id": 1, "result": {}}
