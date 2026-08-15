from datetime import datetime, timezone

from fastapi.testclient import TestClient
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.db.models import AuditLog


def test_list_audit_log():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(AuditLog(username="alice", action="retry", target_type="task", target_id="t1", created_at=datetime.now(timezone.utc)))
        session.commit()

    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    response = client.get("/api/audit")

    assert response.status_code == 200
    assert response.json()[0]["action"] == "retry"
