from unittest.mock import patch

from fastapi.testclient import TestClient
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.db.models import TaskRun


def _client_and_session():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(TaskRun(task_id="t1", task_name="demo.echo", status="failed", args="[]", kwargs="{}"))
        session.commit()
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory


def test_list_tasks_requires_auth():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    response = TestClient(app).get("/api/tasks")
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
