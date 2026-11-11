"""End-to-end happy path against real worker, beat, events and dashboard processes."""
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import ROOT, _exe, eventually, mcp_session

pytestmark = pytest.mark.e2e


def _runs(client, **params):
    r = client.get("/api/tasks", params={"limit": 200, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_dashboard_serves_spa_and_guards_api(stack, client):
    with httpx.Client(base_url=stack.base_url, timeout=20) as anon:
        r = anon.get("/")
        assert r.status_code == 200
        assert "<div id=" in r.text or "<script" in r.text
        assets = re.findall(r'(?:src|href)="([^"]*?/assets/[^"]+\.js)"', r.text)
        assert assets, r.text[:500]
        src = assets[0]
        js = anon.get(src if src.startswith("http") else "/assets/" + src.split("/assets/", 1)[1])
        assert js.status_code == 200
        assert anon.get("/api/tasks").status_code == 401
    me = client.get("/api/whoami").json()
    assert me["username"] == "admin" and me["role"] == "admin"
    spec = client.get("/openapi.json")
    assert spec.status_code == 200 and "/api/llm/usage" in spec.json()["paths"]


def test_successful_task(client, start_task, wait_run):
    tid = start_task("demo.add", [2, 3])
    run = wait_run(tid, "succeeded")
    assert run["worker"]
    assert run["runtime_seconds"] is not None
    assert "5" in json.dumps(run)
    row = next(r for r in _runs(client) if r["task_id"] == tid)
    assert row["status"] == "succeeded" and row["runtime_seconds"] is not None


def test_failing_task_failure_groups_and_retry(client, start_task, wait_run):
    tid = start_task("demo.explode")
    run = wait_run(tid, "failed")
    assert "demo failure" in (run["exception"] or "")

    def grouped():
        groups = client.get("/api/analytics/failures").json()["groups"]
        return any(tid in g["task_ids"] for g in groups)
    eventually(grouped, message="failure group")

    r = client.post(f"/api/tasks/{tid}/retry")
    assert r.status_code == 200, r.text
    new_id = r.json()["new_task_id"]
    assert new_id != tid
    new_run = wait_run(new_id, "failed")
    assert new_run["retried_from_id"] == tid
    assert new_id in client.get(f"/api/tasks/{tid}").json()["retried_by"]


def test_progress_and_llm_usage(client, start_task, wait_run):
    pid = start_task("demo.import_rows")
    run = wait_run(pid, "succeeded")
    assert run["progress_total"] == 3 and run["progress_current"] == 3
    assert run["progress_message"]

    lid = start_task("demo.summarize")
    wait_run(lid, "succeeded")

    def usage():
        t = client.get("/api/llm/usage", params={"range": "24h"}).json()["totals"]
        return t if t["calls"] >= 1 else None
    totals = eventually(usage, message="llm usage")
    assert totals["input_tokens"] >= 1200 and totals["output_tokens"] >= 300
    assert totals["cost_usd"] and totals["cost_usd"] > 0
    per_run = client.get(f"/api/llm/runs/{lid}")
    assert per_run.status_code == 200
    assert "example-model" in per_run.text


def test_schedule_fires_and_pause_resume(client):
    def listed():
        return [s for s in client.get("/api/schedules").json() if s["name"] == "heartbeat"]
    eventually(listed, message="schedule listed")

    eventually(lambda: _runs(client, task_name="demo.heartbeat"), timeout=45, message="beat-fired run")
    assert any(r["task_name"] == "demo.heartbeat" for r in _runs(client))

    r = client.post("/api/schedules/heartbeat/pause")
    assert r.status_code == 200 and r.json()["paused"] is True
    assert eventually(lambda: [s for s in client.get("/api/schedules").json()
                               if s["name"] == "heartbeat" and s["paused"]], message="paused")
    r = client.post("/api/schedules/heartbeat/resume")
    assert r.status_code == 200 and r.json()["paused"] is False
    assert [s for s in client.get("/api/schedules").json() if s["name"] == "heartbeat" and not s["paused"]]


def test_workers_and_queues(client):
    workers = client.get("/api/workers").json()["workers"]
    assert any(w["online"] and "demo@" in w["name"] for w in workers)
    queues = client.get("/api/queues").json()
    assert any(q["name"] == "celery" for q in queues["queues"])


def test_alert_channel_and_rule_crud(client):
    ch = client.post("/api/alerts/channels", json={
        "name": "e2e-hook", "kind": "webhook", "config": {"url": "http://127.0.0.1:9/hook"}})
    assert ch.status_code == 201, ch.text
    ch_id = ch.json()["id"]
    rule = client.post("/api/alerts/rules", json={
        "name": "e2e-failures", "kind": "failure_rate", "channel_ids": [ch_id]})
    assert rule.status_code == 201, rule.text
    rule_id = rule.json()["id"]
    assert any(r["id"] == rule_id for r in client.get("/api/alerts/rules").json()["rules"])
    assert any(c["id"] == ch_id for c in client.get("/api/alerts/channels").json()["channels"])
    assert client.delete(f"/api/alerts/rules/{rule_id}").status_code == 200
    assert client.delete(f"/api/alerts/channels/{ch_id}").status_code == 200
    assert not any(r["id"] == rule_id for r in client.get("/api/alerts/rules").json()["rules"])
    assert not any(c["id"] == ch_id for c in client.get("/api/alerts/channels").json()["channels"])


def _call(send, name, arguments=None):
    resp = send("tools/call", {"name": name, "arguments": arguments or {}})
    assert "error" not in resp, resp
    result = resp["result"]
    assert not result["isError"], result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_mcp_viewer_and_operator(stack, client):
    send, close = mcp_session(stack, "viewer")
    try:
        init = send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "e2e", "version": "1"}})
        assert init["result"]["serverInfo"]["name"] == "celery-manager"
        send("notifications/initialized", notify=True)
        names = {t["name"] for t in send("tools/list")["result"]["tools"]}
        assert {"list_runs", "failure_groups"} <= names
        assert "retry_run" not in names and "retry_runs" not in names
        runs = _call(send, "list_runs", {"limit": 100})
        assert runs["total"] >= 1
        assert any(r["task_name"] == "demo.add" for r in runs["items"])
        assert _call(send, "failure_groups")["groups"]
        denied = send("tools/call", {"name": "retry_run", "arguments": {"task_id": "x"}})["result"]
        assert denied["isError"]
    finally:
        close()

    total_before = client.get("/api/tasks", params={"limit": 1}).json()["total"]
    failed = next(r["task_id"] for r in _runs(client, status="failed"))
    send, close = mcp_session(stack, "operator")
    try:
        send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                            "clientInfo": {"name": "e2e", "version": "1"}})
        send("notifications/initialized", notify=True)
        names = {t["name"] for t in send("tools/list")["result"]["tools"]}
        assert "retry_runs" in names
        dry = _call(send, "retry_runs", {"task_ids": [failed]})
        assert dry.get("dry_run") is True and dry["count"] == 1
    finally:
        close()
    after = client.get("/api/tasks", params={"limit": 1}).json()["total"]
    # Beat may add its own tick runs; a dry run must add nothing for the failed task.
    assert len([r for r in _runs(client) if r.get("task_name") == "demo.explode"]) == 2
    assert after >= total_before


def test_cli_commands(stack):
    doc = stack.cli("doctor", "--json", "--config", "project:config", check=False)
    assert isinstance(json.loads(doc.stdout)["checks"], list), doc.stdout + doc.stderr
    spec = json.loads(stack.cli("openapi").stdout)
    assert "/api/llm/usage" in spec["paths"]
    stack.cli("prune", "--dry-run", "--db-url", stack.db_url)
    stack.cli("migrate", "--check", "--db-url", stack.db_url)


def test_packaging_facts():
    import celery_manager
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert celery_manager.__version__ == version
    assert (ROOT / "docs" / "llms.txt").exists()
    done = subprocess.run([sys.executable, "scripts/build_llms_txt.py", "--check"], check=False, cwd=ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert Path(_exe("celery-manager")).name
