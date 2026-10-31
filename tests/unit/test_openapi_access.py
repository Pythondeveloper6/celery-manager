"""The API description is for signed-in users; interactive docs are off."""
import json

from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig


def _client():
    app, sessions = create_app(CeleryManagerConfig(celery_app=Celery("oa"), db_url="sqlite:///:memory:",
                                                   secret_key="test-secret"))
    with sessions() as session:
        create_user(session, "viewer", "pw", role="viewer")
    return TestClient(app, base_url="https://testserver")


def test_openapi_document_requires_sign_in():
    client = _client()
    assert client.get("/openapi.json").status_code == 401
    client.post("/api/auth/login", json={"username": "viewer", "password": "pw"})
    response = client.get("/openapi.json")
    assert response.status_code == 200
    document = response.json()
    assert document["info"]["title"] == "celery-manager"
    assert "/api/v1/tasks" in document["paths"]
    assert "/openapi.json" not in document["paths"]


def test_swagger_and_redoc_pages_are_not_served():
    client = _client()
    client.post("/api/auth/login", json={"username": "viewer", "password": "pw"})
    for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        response = client.get(path)
        assert response.status_code == 404 or "swagger" not in response.text.lower(), path


def test_cli_exports_the_document_offline(tmp_path, capsys):
    from celery_manager.cli import main

    out = tmp_path / "openapi.json"
    try:
        main(["openapi", "--output", str(out)])
    except SystemExit as exc:
        assert exc.code == 0
    document = json.loads(out.read_text())
    assert "/api/v1/tasks" in document["paths"]
    assert document["info"]["version"]
