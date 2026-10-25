from datetime import datetime, timezone

from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import AuditLog


def test_list_audit_log():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(AuditLog(username="alice", action="retry", target_type="task", target_id="t1", created_at=datetime.now(timezone.utc)))
        session.commit()

    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    response = client.get("/api/audit")

    assert response.status_code == 200
    assert response.json()[0]["action"] == "retry"


def _client_with_audit_rows(n):
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        for i in range(n):
            session.add(AuditLog(username="alice", action="retry", target_type="task",
                                 target_id=f"t{i}", created_at=datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc)))
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client


def test_audit_created_at_carries_a_utc_offset():
    """The audit screen renders this with `new Date(...).toLocaleString()`,
    which reads an unmarked timestamp as *local* time — so an unmarked value
    shows every entry shifted by the viewer's UTC offset. Same fix, and same
    reason, as the task endpoints' timestamps."""
    client = _client_with_audit_rows(1)

    created_at = client.get("/api/audit").json()[0]["created_at"]

    assert created_at.endswith(("Z", "+00:00")), created_at
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    assert parsed == datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc)


def test_audit_limit_is_clamped_like_the_task_list():
    """`?limit=-1` used to return the whole table: SQLite reads LIMIT -1 as
    'no limit'. The task list already clamped; this one did not."""
    client = _client_with_audit_rows(120)

    assert len(client.get("/api/audit").json()) == 50
    assert len(client.get("/api/audit", params={"limit": 5}).json()) == 5
    assert len(client.get("/api/audit", params={"limit": -1}).json()) == 1
    assert len(client.get("/api/audit", params={"limit": 0}).json()) == 1
    assert len(client.get("/api/audit", params={"limit": 10_000_000}).json()) == 120
