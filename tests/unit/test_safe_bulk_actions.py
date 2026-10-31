"""Destructive bulk actions: dry runs, typed confirmation and size limits."""
from unittest.mock import MagicMock, patch

from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.tasks import MAX_BULK_RETRY
from celery_manager.db.models import AuditLog, TaskRun
from tests.unit.test_queues import _fake_conn, _queued


def echo(*args, **kwargs):
    return None


def _client(runs=(), **options):
    celery_app = Celery("safe", broker="amqp://guest@localhost//")
    celery_app.task(name="demo.echo", shared=False)(echo)
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret", **options)
    app, sessions = create_app(config)
    with sessions() as session:
        create_user(session, "alice", "pw")
        session.add_all(list(runs))
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, sessions, celery_app


# --- purge ---------------------------------------------------------------------


def test_purge_dry_run_reports_depth_and_would_mark_without_purging():
    client, sessions, celery_app = _client([_queued("q1", "slow"), _queued("q2", "slow"), _queued("q3", "fast")])
    conn = _fake_conn({"slow": (12403, 1)})
    with patch.object(celery_app, "connection_for_read", return_value=conn):
        response = client.post("/api/queues/slow/purge", json={"dry_run": True})
    assert response.status_code == 200, response.text
    assert response.json() == {"queue": "slow", "dry_run": True, "messages_waiting": 12403,
                               "runs_that_would_be_marked_revoked": 2}
    assert all(not channel.queue_purge.called for channel in conn.channels)
    with sessions() as session:
        assert session.query(AuditLog).filter_by(action="purge").count() == 0
        assert {row.status for row in session.query(TaskRun)} == {"queued"}


def test_purge_dry_run_of_an_unknown_queue_is_404():
    client, _, celery_app = _client()
    with patch.object(celery_app, "connection_for_read", return_value=_fake_conn({})):
        assert client.post("/api/queues/nope/purge", json={"dry_run": True}).status_code == 404


def test_purge_with_a_mismatched_confirmation_is_refused_before_the_broker():
    client, sessions, celery_app = _client()
    conn = _fake_conn({}, purge_result=5)
    with patch.object(celery_app, "connection_for_read", return_value=conn):
        response = client.post("/api/queues/slow/purge", json={"confirm": "slo"})
    assert response.status_code == 400
    assert conn.channels == []
    with sessions() as session:
        assert session.query(AuditLog).filter_by(action="purge").count() == 0


def test_purge_with_the_exact_name_proceeds():
    client, _, celery_app = _client()
    with patch.object(celery_app, "connection_for_read", return_value=_fake_conn({}, purge_result=5)):
        response = client.post("/api/queues/slow/purge", json={"confirm": "slow"})
    assert response.status_code == 200 and response.json()["messages_removed"] == 5


def test_required_confirmation_refuses_a_bare_purge():
    client, _, celery_app = _client(require_typed_confirmation=True)
    conn = _fake_conn({}, purge_result=5)
    with patch.object(celery_app, "connection_for_read", return_value=conn):
        assert client.post("/api/queues/slow/purge").status_code == 400
        assert client.post("/api/queues/slow/purge", json={"confirm": "slow"}).status_code == 200


def test_bodyless_purge_keeps_working_by_default():
    client, _, celery_app = _client()
    with patch.object(celery_app, "connection_for_read", return_value=_fake_conn({}, purge_result=1)):
        assert client.post("/api/queues/slow/purge").status_code == 200


# --- bulk retry ----------------------------------------------------------------


def _failed(task_id):
    return TaskRun(task_id=task_id, task_name="demo.echo", status="failed", args="[]", kwargs="{}")


def test_bulk_retry_dry_run_validates_and_publishes_nothing():
    client, sessions, celery_app = _client([_failed("f1"), _failed("f2")])
    with patch.object(celery_app, "send_task") as send:
        response = client.post("/api/tasks/retry-bulk", json={"task_ids": ["f1", "f2"], "dry_run": True})
        missing = client.post("/api/tasks/retry-bulk", json={"task_ids": ["f1", "zz"], "dry_run": True})
    assert response.status_code == 200
    assert response.json() == {"dry_run": True, "would_retry": ["f1", "f2"], "count": 2}
    assert missing.status_code == 404
    send.assert_not_called()
    with sessions() as session:
        assert session.query(TaskRun).count() == 2


def test_bulk_retry_confirm_count_must_match():
    client, _, celery_app = _client([_failed("f1"), _failed("f2")])
    with patch.object(celery_app, "send_task", return_value=MagicMock(id="new")) as send:
        assert client.post("/api/tasks/retry-bulk", json={"task_ids": ["f1", "f2"], "confirm_count": 3}).status_code == 400
        send.assert_not_called()


def test_required_confirmation_applies_to_bulk_retry():
    client, _, _celery_app = _client([_failed("f1")], require_typed_confirmation=True)
    with patch("celery_manager.control.tasks.retry_task", return_value="new") as retry:
        assert client.post("/api/tasks/retry-bulk", json={"task_ids": ["f1"]}).status_code == 400
        retry.assert_not_called()
        ok = client.post("/api/tasks/retry-bulk", json={"task_ids": ["f1"], "confirm_count": 1})
    assert ok.status_code == 200 and ok.json() == {"new_task_ids": ["new"]}


def test_bulk_retry_refuses_oversized_and_duplicate_batches():
    client, _, _celery_app = _client([_failed("f1")])
    with patch("celery_manager.control.tasks.retry_task") as retry:
        too_many = client.post("/api/tasks/retry-bulk", json={"task_ids": [f"x{i}" for i in range(MAX_BULK_RETRY + 1)]})
        duplicate = client.post("/api/tasks/retry-bulk", json={"task_ids": ["f1", "f1"]})
    assert too_many.status_code == 400 and duplicate.status_code == 400
    retry.assert_not_called()
