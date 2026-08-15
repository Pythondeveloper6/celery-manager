from fastapi.testclient import TestClient
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.db.session import get_session_factory


def _client():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "correct-horse-battery-staple")
    return TestClient(app)


def test_login_succeeds_with_correct_credentials():
    client = _client()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    assert response.status_code == 200
    assert "session" in response.cookies


def test_login_fails_with_wrong_password():
    client = _client()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert response.status_code == 401


def test_protected_route_requires_session():
    client = _client()
    response = client.get("/api/whoami")
    assert response.status_code == 401


def test_protected_route_succeeds_after_login():
    client = _client()
    client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    response = client.get("/api/whoami")
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}
