"""Start-from-the-UI support: registered-task listing, start options, and
retry with edited arguments."""
from unittest.mock import MagicMock, patch

from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import AuditLog, TaskRun

SEND = "celery_manager.control.tasks.Celery.send_task"


def _registered_client():
    app = Celery("reg")

    @app.task(name="demo.greet")
    def greet(name, punctuation="!", *, loud: bool = False):
        """Say hello.

        More detail here.
        """

    # A registry entry with no inspectable run function.
    class Broken:
        queue = None

        @property
        def run(self):
            raise RuntimeError("no signature for you")

    app.tasks["demo.broken"] = Broken()
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="test-secret", allow_queue_override=True)
    fastapi_app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(TaskRun(task_id="t1", task_name="demo.greet", status="failed", args='["x"]', kwargs='{"loud": true}'))
        session.commit()
    client = TestClient(fastapi_app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory


def test_registered_requires_auth():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    assert TestClient(app, base_url="https://testserver").get("/api/tasks/registered").status_code == 401


def test_registered_lists_user_tasks_without_celery_internals():
    client, _ = _registered_client()
    items = client.get("/api/tasks/registered").json()["items"]
    names = [i["name"] for i in items]
    assert "demo.greet" in names
    assert not any(n.startswith("celery.") for n in names)
    greet = next(i for i in items if i["name"] == "demo.greet")
    assert greet["doc"] == "Say hello."
    sig = {p["name"]: p for p in greet["signature"]}
    assert sig["name"]["required"] is True
    assert sig["punctuation"]["default"] == "!" and sig["punctuation"]["required"] is False
    assert sig["loud"]["kind"] == "keyword_only" and sig["loud"]["annotation"] == "bool"


def test_registered_survives_uninspectable_task():
    client, _ = _registered_client()
    response = client.get("/api/tasks/registered")
    assert response.status_code == 200
    broken = next(i for i in response.json()["items"] if i["name"] == "demo.broken")
    assert broken["signature"] is None


def test_start_forwards_queue_countdown_and_audits_them():
    client, session_factory = _registered_client()
    with patch(SEND, return_value=MagicMock(id="n1"), create=True) as send:
        response = client.post("/api/tasks/start", json={
            "task_name": "demo.greet", "args": ["a"], "queue": "slow", "countdown": 5,
        })
    assert response.status_code == 200
    assert send.call_args.kwargs["queue"] == "slow" and send.call_args.kwargs["countdown"] == 5
    with session_factory() as session:
        row = session.query(AuditLog).filter_by(action="start").one()
        assert '"queue": "slow"' in row.details


def test_start_without_options_sends_no_extra_options():
    client, _ = _registered_client()
    with patch(SEND, return_value=MagicMock(id="n2"), create=True) as send:
        client.post("/api/tasks/start", json={"task_name": "demo.greet"})
    assert set(send.call_args.kwargs) == {"args", "kwargs"}


def test_start_rejects_negative_countdown():
    client, _ = _registered_client()
    assert client.post("/api/tasks/start", json={"task_name": "demo.greet", "countdown": -1}).status_code == 422


def test_retry_without_body_keeps_stored_arguments_and_audit_is_plain():
    client, session_factory = _registered_client()
    with patch(SEND, return_value=MagicMock(id="r1"), create=True) as send:
        response = client.post("/api/tasks/t1/retry")
    assert response.json() == {"new_task_id": "r1"}
    assert send.call_args.kwargs["args"] == ["x"] and send.call_args.kwargs["kwargs"] == {"loud": True}
    with session_factory() as session:
        row = session.query(AuditLog).filter_by(action="retry").one()
        assert "args_edited" not in row.details


def test_retry_with_edited_arguments_records_lineage_and_audit():
    client, session_factory = _registered_client()
    with patch(SEND, return_value=MagicMock(id="r2"), create=True) as send:
        response = client.post("/api/tasks/t1/retry", json={"args": ["y"], "kwargs": {"loud": False}})
    assert response.status_code == 200
    assert send.call_args.kwargs["args"] == ["y"] and send.call_args.kwargs["kwargs"] == {"loud": False}
    with session_factory() as session:
        new = session.query(TaskRun).filter_by(task_id="r2").one()
        assert new.retried_from_id == "t1"
        assert new.args == '["y"]'
        row = session.query(AuditLog).filter_by(action="retry").one()
        assert '"args_edited": true' in row.details and '"y"' in row.details


def test_retry_edit_only_kwargs_keeps_stored_args():
    client, _ = _registered_client()
    with patch(SEND, return_value=MagicMock(id="r3"), create=True) as send:
        client.post("/api/tasks/t1/retry", json={"kwargs": {}})
    assert send.call_args.kwargs["args"] == ["x"] and send.call_args.kwargs["kwargs"] == {}


def test_retry_rejects_wrong_types():
    client, _ = _registered_client()
    assert client.post("/api/tasks/t1/retry", json={"args": {"a": 1}}).status_code == 422
    assert client.post("/api/tasks/t1/retry", json={"kwargs": [1]}).status_code == 422


def test_retry_unknown_run_with_body_is_404():
    client, _ = _registered_client()
    assert client.post("/api/tasks/nope/retry", json={"args": []}).status_code == 404


def test_start_seeds_a_queued_run_so_its_page_opens_immediately():
    client, _ = _registered_client()
    with patch(SEND, return_value=MagicMock(id="seed-1"), create=True):
        client.post("/api/tasks/start", json={"task_name": "demo.greet", "args": ["a"], "kwargs": {"loud": True}, "queue": "q1"})
    body = client.get("/api/tasks/seed-1").json()
    assert body["status"] == "queued" and body["queue"] == "q1" and body["task_name"] == "demo.greet"
