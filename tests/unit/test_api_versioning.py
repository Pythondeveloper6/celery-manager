from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app, iter_api_routes
from celery_manager.auth.external import ExternalUser
from celery_manager.config import CeleryManagerConfig


def test_v1_matches_legacy_read_auth_and_role_boundaries():
    config = CeleryManagerConfig(celery_app=Celery("v1"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    client = TestClient(app)
    assert client.get("/api/v1/tasks").status_code == 401
    config.authenticate_request = lambda request: ExternalUser("viewer", "viewer")
    assert client.get("/api/v1/tasks").json() == client.get("/api/tasks").json()
    assert client.post("/api/v1/tasks/start", json={"task_name": "bad"}).status_code == 403
    assert client.post("/api/v1/auth/logout", headers={"Origin": "https://evil.test"}).status_code == 403
    schema = app.openapi()
    assert "/api/v1/tasks" in schema["paths"]
    ids = [entry["operationId"] for path in schema["paths"].values() for entry in path.values() if "operationId" in entry]
    assert len(ids) == len(set(ids))
    legacy = {path for path, _, _ in iter_api_routes(app.routes) if path.startswith("/api/") and not path.startswith("/api/v1/")}
    versioned = {path for path, _, _ in iter_api_routes(app.routes) if path.startswith("/api/v1/")}
    assert {path.replace("/api/", "/api/v1/", 1) for path in legacy} == versioned
