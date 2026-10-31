"""The registered-task listing carries each task's argument JSON Schema, so
the Run now dialog can render a form instead of raw JSON."""
from typing import Literal

from celery import Celery
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig


class ResizeArgs(BaseModel):
    image_id: int = Field(ge=1, description="Image to resize")
    size: Literal["small", "large"] = "small"
    sharpen: bool = False


def _client():
    app = Celery("forms")

    @app.task(name="media.resize")
    def resize(image_id, size="small", sharpen=False):
        pass

    @app.task(name="media.plain")
    def plain(x):
        pass

    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="test-secret",
                                 args_schemas={"media.resize": ResizeArgs})
    fastapi_app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "ann", "pw", role="viewer")
    client = TestClient(fastapi_app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "ann", "password": "pw"})
    return client


def test_registered_tasks_include_their_argument_schema():
    items = {i["name"]: i for i in _client().get("/api/tasks/registered").json()["items"]}
    schema = items["media.resize"]["args_schema"]
    assert schema["required"] == ["image_id"]
    assert schema["properties"]["image_id"]["type"] == "integer"
    assert schema["properties"]["size"]["enum"] == ["small", "large"]
    assert items["media.plain"]["args_schema"] is None
