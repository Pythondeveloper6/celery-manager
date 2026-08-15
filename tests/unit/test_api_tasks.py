from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.db.models import AuditLog, TaskRun


def _client_and_session():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(TaskRun(task_id="t1", task_name="demo.echo", status="failed", args="[]", kwargs="{}"))
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory


def test_list_tasks_requires_auth():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    response = TestClient(app, base_url="https://testserver").get("/api/tasks")
    assert response.status_code == 401


def test_list_tasks_returns_seeded_run():
    client, _ = _client_and_session()
    response = client.get("/api/tasks")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["task_id"] == "t1"


def test_get_task_detail():
    client, _ = _client_and_session()
    response = client.get("/api/tasks/t1")
    assert response.status_code == 200
    assert response.json()["task_name"] == "demo.echo"


def test_get_task_detail_404_when_missing():
    client, _ = _client_and_session()
    response = client.get("/api/tasks/does-not-exist")
    assert response.status_code == 404


def test_retry_task_endpoint():
    client, _ = _client_and_session()
    with patch("celery_manager.api.routes.tasks.retry_task", return_value="t1-retry") as mock_retry:
        response = client.post("/api/tasks/t1/retry")
    assert response.status_code == 200
    assert response.json() == {"new_task_id": "t1-retry"}
    mock_retry.assert_called_once()


# --- I2: audit coverage. `audit.record` used to be called only from
# retry_task, so start and cancel — both destructive operator actions taken
# through the dashboard's own API — left no trace of who did what. ---


def test_start_task_endpoint_writes_an_audit_row():
    client, session_factory = _client_and_session()
    with patch("celery_manager.control.tasks.Celery.send_task", return_value=MagicMock(id="new-1"), create=True):
        response = client.post("/api/tasks/start", json={"task_name": "demo.echo", "args": [1], "kwargs": {}})

    assert response.status_code == 200
    assert response.json() == {"task_id": "new-1"}

    with session_factory() as session:
        rows = session.query(AuditLog).filter_by(action="start").all()
        assert len(rows) == 1
        assert rows[0].username == "alice"
        assert rows[0].target_type == "task"
        assert rows[0].target_id == "new-1"
        assert "demo.echo" in rows[0].details


def test_cancel_task_endpoint_writes_an_audit_row():
    client, session_factory = _client_and_session()
    with patch("celery_manager.control.tasks.Celery.control", create=True):
        response = client.post("/api/tasks/t1/cancel?terminate=true")

    assert response.status_code == 200

    with session_factory() as session:
        rows = session.query(AuditLog).filter_by(action="cancel").all()
        assert len(rows) == 1
        assert rows[0].username == "alice"
        assert rows[0].target_type == "task"
        assert rows[0].target_id == "t1"
        assert "true" in rows[0].details.lower()


def test_retry_does_not_double_log_as_both_retry_and_start():
    """`retry_task` dispatches via `start_task`; auditing unconditionally
    inside `start_task` would record every retry twice under two names."""
    client, session_factory = _client_and_session()
    with patch("celery_manager.control.tasks.Celery.send_task", return_value=MagicMock(id="t1-retry"), create=True):
        response = client.post("/api/tasks/t1/retry")

    assert response.status_code == 200
    with session_factory() as session:
        actions = [r.action for r in session.query(AuditLog).all()]
        assert actions == ["retry"]
