"""Tenant reads are SQL-scoped; global operations and event channels fail closed."""
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.external import ExternalUser
from celery_manager.db.models import FailureResolutionHistory, TaskRun


@pytest.fixture
def tenant_app():
    app = Celery("tenants", broker="memory://")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="tenant-test-key",
                                authenticate_request=lambda request: ExternalUser("admin", "admin", "same-user"),
                                tenant_resolver=lambda request, principal: request.headers.get("x-trusted-tenant"))
    api, sessions = create_app(config)
    now = datetime.now(timezone.utc)
    with sessions() as session:
        for task_id, tenant in (("a1", "a"), ("b1", "b"), ("a2", "a"), ("b2", "b"), ("legacy", None)):
            session.add(TaskRun(task_id=task_id, task_name="shared.task", status="failed", tenant_id=tenant,
                                args=json.dumps([tenant]), kwargs="{}", exception=f"{tenant}-private-error", started_at=now,
                                finished_at=now, initiator="same-user"))
        session.add(FailureResolutionHistory(task_id="b1", state="ignored", actor="b-admin", note="b-private-note", created_at=now))
        session.commit()
    return app, config, sessions, TestClient(api, base_url="https://testserver")


def test_two_tenants_scoped_before_paging_and_unscoped_workers_unchanged(tenant_app):
    _, _, sessions, client = tenant_app
    for tenant in ("a", "b"):
        response = client.get("/api/tasks?limit=1&offset=1&tenant_id=other", headers={"x-trusted-tenant": tenant})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["total"] == 2 and len(value["items"]) == 1
        assert value["items"][0]["task_id"].startswith(tenant)
        assert client.get("/api/tasks?started_by_me=true", headers={"x-trusted-tenant": tenant}).json()["total"] == 2
    with sessions() as session:
        assert session.query(TaskRun).count() == 5
        session.add(TaskRun(task_id="global-writer", task_name="shared.task", status="queued", tenant_id="b"))
        session.commit()
    assert client.get("/api/tasks", headers={"x-trusted-tenant": "a"}).json()["total"] == 2


def test_guessed_ids_history_workflow_and_lineage_do_not_cross_scope(tenant_app):
    _, _, sessions, client = tenant_app
    with sessions() as session:
        row = session.query(TaskRun).filter_by(task_id="a1").one()
        row.parent_id = row.root_id = row.retried_from_id = "b1"
        row.group_id = "b-private-group"
        session.commit()
    headers = {"x-trusted-tenant": "a"}
    for prefix in ("/api", "/api/v1"):
        assert client.get(prefix + "/tasks/b1", headers=headers).status_code == 404
        assert client.get(prefix + "/tasks/legacy", headers=headers).status_code == 404
        assert client.get(prefix + "/tasks/b1/workflow", headers=headers).status_code == 404
        history = client.get(prefix + "/dead-letters/b1/history", headers=headers)
        assert history.status_code == 200 and history.json()["items"] == []
    detail = client.get("/api/tasks/a1", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["parent_id"] is None and detail.json()["root_id"] is None
    assert detail.json()["retried_from_id"] is None and detail.json()["group_id"] is None
    workflow = client.get("/api/tasks/a1/workflow", headers=headers).json()
    assert workflow["root_id"] == "a1"
    assert [node["task_id"] for node in workflow["nodes"]] == ["a1"]
    assert "b1" not in json.dumps(workflow)


def test_export_stream_and_failure_summary_are_scoped(tenant_app):
    _, _, _, client = tenant_app
    headers = {"x-trusted-tenant": "a"}
    for fmt in ("csv", "json"):
        response = client.get(f"/api/tasks/export?format={fmt}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["x-export-total"] == "2"
        assert "b-private-error" not in response.text and "legacy" not in response.text
        assert "a-private-error" in response.text
    inbox = client.get("/api/dead-letters", headers=headers)
    assert inbox.status_code == 200, inbox.text
    assert {item["task_id"] for item in inbox.json()["items"]} == {"a1", "a2"}
    assert "b-private" not in inbox.text


@pytest.mark.parametrize("path", ["/api/stream", "/api/v1/stream", "/metrics", "/api/analytics", "/api/workers", "/api/queues", "/api/schedules", "/api/settings", "/api/audit", "/api/alerts/rules", "/api/locks", "/openapi.json", "/api/health/probes", "/api/health/schedulers", "/api/health/beat", "/api/insights"])
def test_global_views_and_events_are_denied_even_for_admin(tenant_app, path):
    _, _, _, client = tenant_app
    response = client.get(path, headers={"x-trusted-tenant": "a"})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("path,body", [("/api/tasks/start", {"task_name": "shared.task"}), ("/api/tasks/a1/retry", {}), ("/api/tasks/b1/cancel?force=true", {}), ("/api/tasks/retry-bulk", {"task_ids": ["a1", "b1"]}), ("/api/tasks/cancel-bulk", {"filter": {}}), ("/api/dead-letters/rerun", {"task_ids": ["a1"]}), ("/api/dead-letters/a1/ignore", {})])
def test_all_operational_writes_denied_without_dispatch(tenant_app, path, body):
    app, _, _, client = tenant_app
    with patch.object(app, "send_task") as send, patch.object(app.control, "revoke") as revoke:
        response = client.post(path, json=body, headers={"x-trusted-tenant": "a"})
        assert response.status_code == 403, response.text
        send.assert_not_called()
        revoke.assert_not_called()


def test_missing_scope_is_not_replaced_by_query_parameter_or_admin(tenant_app):
    _, config, _, client = tenant_app
    assert client.get("/api/tasks?tenant_id=a").status_code == 403
    assert client.get("/api/tasks", headers={"x-trusted-tenant": " "}).status_code == 403
    async def resolver(request, principal):
        assert principal.role == "admin"
        return "b"
    config.tenant_resolver = resolver
    assert {row["task_id"] for row in client.get("/api/tasks").json()["items"]} == {"b1", "b2"}
    def broken(request, principal):
        raise RuntimeError("host-private-detail")
    config.tenant_resolver = broken
    response = client.get("/api/tasks")
    assert response.status_code == 503 and "host-private-detail" not in response.text


def test_capabilities_advertise_readonly_and_polling(tenant_app):
    _, _, _, client = tenant_app
    options = client.get("/api/auth/options").json()
    assert options["tenant_scoped"] is True and options["read_only"] is True and options["live_stream"] == "poll"
    assert client.get("/api/whoami", headers={"x-trusted-tenant": "a"}).json()["tenant_id"] == "a"


def test_context_is_isolated_between_concurrent_requests(tenant_app):
    from concurrent.futures import ThreadPoolExecutor
    _, _, _, client = tenant_app
    def read(tenant):
        for _ in range(10):
            response = client.get("/api/tasks?limit=1", headers={"x-trusted-tenant": tenant})
            assert response.status_code == 200
            assert response.json()["total"] == 2
            assert response.json()["items"][0]["task_id"].startswith(tenant)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(read, ("a", "b")))


def test_cross_tenant_retry_alias_does_not_hide_visible_failure(tenant_app):
    _, _, sessions, client = tenant_app
    with sessions() as session:
        session.query(TaskRun).filter_by(task_id="b2").one().retried_from_id = "a2"
        session.query(TaskRun).filter_by(task_id="b1").one().task_name = "private.other.task"
        session.commit()
    response = client.get("/api/dead-letters", headers={"x-trusted-tenant": "a"})
    assert response.status_code == 200
    assert {row["task_id"] for row in response.json()["items"]} == {"a1", "a2"}
    assert "private.other.task" not in response.text


def test_invalid_resolver_rejected_before_runtime():
    with pytest.raises(TypeError, match="tenant_resolver"):
        CeleryManagerConfig(celery_app=Celery("invalid"), tenant_resolver="query-param")


def test_new_static_task_routes_are_not_implicitly_trusted(tenant_app):
    _app, config, _sessions, _client = tenant_app
    api, _ = create_app(config)
    # Insert ahead of the existing dynamic task-id route to mirror a future
    # static endpoint winning route resolution. It has no reviewed contract.
    from fastapi.routing import APIRoute
    api.router.routes.insert(0, APIRoute("/api/tasks/new-global-view", lambda: {"private":"all tenants"}, methods=["GET"]))
    client = TestClient(api, base_url="https://testserver")
    response = client.get("/api/tasks/new-global-view", headers={"x-trusted-tenant":"a"})
    assert response.status_code == 403


def test_trusted_scope_preserves_exact_unicode_case_and_trailing_spaces(tenant_app):
    _, config, sessions, client = tenant_app
    tenants = ("Case", "case", "cáse", "ca\u0301se", "case ", "شركة")
    with sessions() as session:
        for index, tenant in enumerate(tenants):
            session.add(TaskRun(task_id=f"exact-{index}", task_name="host.command", status="failed", tenant_id=tenant))
        session.commit()
    for index, tenant in enumerate(tenants):
        config.tenant_resolver = lambda request, principal, value=tenant: value
        response = client.get("/api/tasks")
        assert response.status_code == 200, response.text
        assert [row["task_id"] for row in response.json()["items"]] == [f"exact-{index}"]
        assert client.get("/api/whoami").json()["tenant_id"] == tenant


def test_oversized_utf16_tenant_scope_fails_closed(tenant_app):
    _, config, _, client = tenant_app
    config.tenant_resolver = lambda request, principal: "😀" * 128
    assert client.get("/api/tasks").status_code == 403
    config.tenant_resolver = lambda request, principal: "😀" * 127 + "x"
    response = client.get("/api/tasks")
    assert response.status_code == 200 and response.json()["items"] == []
