from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.external import ExternalUser
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.tasks import count_task_runs, list_task_runs
from celery_manager.db.models import TaskRun


def test_stable_subject_filters_list_export_and_bulk_cancel(monkeypatch):
    app, sessions = create_app(CeleryManagerConfig(
        celery_app=Celery("initiator"), db_url="sqlite:///:memory:", secret_key="test-secret",
        authenticate_request=lambda request: ExternalUser("display-name", "operator", subject="issuer:user-1"),
    ))
    with sessions() as session:
        session.add_all([TaskRun(task_id=key, task_name="demo", status="queued", initiator=actor)
                         for key, actor in [("mine", "issuer:user-1"), ("other", "display-name"), ("unknown", None)]])
        session.commit()
        assert count_task_runs(session, initiator="issuer:user-1") == 1
        assert [r.task_id for r in list_task_runs(session, initiator="issuer:user-1")] == ["mine"]
    client = TestClient(app)
    page = client.get("/api/tasks?started_by_me=true").json()
    assert page["total"] == 1
    assert page["items"][0]["task_id"] == "mine"
    data = client.get("/api/tasks/export?format=json&started_by_me=true").json()
    assert [r["task_id"] for r in data] == ["mine"]
    response = client.post("/api/tasks/cancel-bulk", json={"filter": {"started_by_me": True}, "dry_run": True})
    assert response.status_code == 200
    assert response.json()["matched"] == 1
