"""Queue depth, purge, and bulk cancel.

The broker is mocked at `celery_app.connection_for_read` — the one seam the
control layer uses — so these exercise our classification of broker answers
(depth, consumers, NOT_FOUND, refusals, timeouts) without a running broker.
"""

import json
import threading
from collections import namedtuple
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from kombu import Queue
from kombu.transport.virtual import Transport as VirtualTransport

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import queues as queues_mod
from celery_manager.control.queues import configured_queue_names
from celery_manager.db.models import AuditLog, TaskRun

DeclareOk = namedtuple("DeclareOk", "queue message_count consumer_count")


class NotFoundError(Exception):
    code = 404


@pytest.fixture(autouse=True)
def _clear_worker_cache():
    queues_mod._worker_cache.clear()
    yield
    queues_mod._worker_cache.clear()


def _app(broker="amqp://guest@localhost//", runs=()):
    celery_app = Celery("test", broker=broker)
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        for run in runs:
            session.add(run)
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory, celery_app


def _fake_conn(depths: dict, transport=None, purge_result=None, purge_error=None):
    """A kombu-ish connection whose channels answer passive declares from `depths`.

    A value that is an Exception is raised instead, the way a broker refuses.
    """
    conn = MagicMock()
    conn.transport = transport if transport is not None else MagicMock(driver_type="amqp")
    channels = []

    def make_channel():
        channel = MagicMock()

        def declare(queue, passive):
            assert passive is True  # never create queues by looking at them
            answer = depths.get(queue, NotFoundError(f"NOT_FOUND - no queue {queue!r}"))
            if isinstance(answer, Exception):
                raise answer
            return DeclareOk(queue, *answer)

        channel.queue_declare.side_effect = declare
        if purge_error is not None:
            channel.queue_purge.side_effect = purge_error
        else:
            channel.queue_purge.return_value = purge_result
        channels.append(channel)
        return channel

    conn.channel.side_effect = make_channel
    conn.channels = channels
    return conn


def _queued(task_id, queue, status="queued", eta=None, task_name="demo.echo"):
    return TaskRun(task_id=task_id, task_name=task_name, status=status, queue=queue, eta=eta, args="[]", kwargs="{}")


# --- discovery ---------------------------------------------------------------


def test_configured_queue_names_reads_default_queues_and_routes():
    app = Celery("t")
    app.conf.task_default_queue = "main"
    app.conf.task_queues = [Queue("main"), Queue("reports")]
    app.conf.task_routes = [{"a.task": "mail"}, {"b.task": {"queue": "slow"}}, lambda *a, **k: None]
    found = configured_queue_names(app)
    assert found == {
        "main": {"default", "config"},
        "reports": {"config"},
        "mail": {"routes"},
        "slow": {"routes"},
    }


def test_configured_queue_names_falls_back_to_celery():
    assert configured_queue_names(Celery("t")) == {"celery": {"default"}}


# --- listing -----------------------------------------------------------------


def test_list_queues_requires_auth():
    config = CeleryManagerConfig(celery_app=Celery("t"), db_url="sqlite:///:memory:", secret_key="s")
    app, _ = create_app(config)
    assert TestClient(app, base_url="https://testserver").get("/api/queues").status_code == 401


def test_list_queues_reports_depth_consumers_and_recorded_counts():
    client, _, celery_app = _app(runs=[
        _queued("q1", "celery"), _queued("q2", "celery"), _queued("q3", "reports"),
        _queued("s1", "reports", status="started"),
        _queued("n1", None),
    ])
    conn = _fake_conn({"celery": (12, 2), "reports": (0, 1), "live": (4, 3)})
    inspector = MagicMock()
    inspector.active_queues.return_value = {"w1@host": [{"name": "celery"}, {"name": "live"}]}
    with patch.object(celery_app, "connection_for_read", return_value=conn), \
            patch.object(celery_app.control, "inspect", return_value=inspector):
        body = client.get("/api/queues").json()

    assert body["broker"] == {"transport": "amqp", "error": None, "unsupported": False, "workers_replied": True}
    by_name = {q["name"]: q for q in body["queues"]}
    assert body["queues"][0]["name"] == "celery"  # default queue first
    assert by_name["celery"]["depth"] == 12
    assert by_name["celery"]["consumers"] == 2
    assert by_name["celery"]["recorded_queued"] == 2
    assert by_name["celery"]["sources"] == ["default", "runs", "workers"]
    assert by_name["reports"]["recorded_queued"] == 1  # the started run is not queued
    assert by_name["live"] == {
        "name": "live", "sources": ["workers"], "depth": 4, "consumers": 3,
        "exists": True, "reason": None, "recorded_queued": 0,
    }
    # Runs with no queue are counted, not dropped or guessed into "celery".
    assert body["recorded_queued_unassigned"] == 1
    conn.release.assert_called_once()


def test_missing_queue_is_empty_and_a_refusal_is_unknown():
    client, _, celery_app = _app()
    celery_app.conf.task_routes = {"a": "gone", "b": "forbidden"}
    conn = _fake_conn({"celery": (1, 1), "forbidden": RuntimeError("ACCESS_REFUSED")})
    with patch.object(celery_app, "connection_for_read", return_value=conn):
        by_name = {q["name"]: q for q in client.get("/api/queues?workers=false").json()["queues"]}

    assert by_name["gone"]["depth"] == 0
    assert by_name["gone"]["exists"] is False
    assert by_name["gone"]["reason"]
    assert by_name["forbidden"]["depth"] is None
    assert "ACCESS_REFUSED" in by_name["forbidden"]["reason"]
    # A fresh channel per queue: a 404 closes an AMQP channel, and one dead
    # channel must not take every later queue down with it.
    assert len(conn.channels) == 3


def test_virtual_transport_reports_unknown_consumers():
    class FakeRedis(VirtualTransport):
        driver_type = "redis"

        def __init__(self):
            pass

    client, _, celery_app = _app()
    conn = _fake_conn({"celery": (7, 0)}, transport=FakeRedis())
    with patch.object(celery_app, "connection_for_read", return_value=conn):
        queue = client.get("/api/queues?workers=false").json()["queues"][0]
    # kombu's virtual transports always say 0 consumers; that is "unknown".
    assert queue["depth"] == 7
    assert queue["consumers"] is None


def test_memory_broker_is_reported_unsupported_not_failed():
    client, _, _ = _app(broker="memory://", runs=[_queued("q1", "celery")])
    response = client.get("/api/queues")
    assert response.status_code == 200
    body = response.json()
    assert body["broker"]["unsupported"] is True
    assert body["broker"]["transport"] == "memory"
    assert "in-memory" in body["broker"]["error"]
    queue = body["queues"][0]
    assert queue["depth"] is None
    assert queue["reason"] == body["broker"]["error"]
    # The recorded number is still served when the broker can't be asked.
    assert queue["recorded_queued"] == 1


def test_unreachable_broker_still_answers_200_with_the_reason():
    client, _, celery_app = _app(runs=[_queued("q1", "celery")])
    conn = MagicMock()
    conn.connect.side_effect = ConnectionRefusedError("[Errno 61] Connection refused")
    with patch.object(celery_app, "connection_for_read", return_value=conn):
        response = client.get("/api/queues")
    assert response.status_code == 200
    body = response.json()
    assert "Connection refused" in body["broker"]["error"]
    assert body["queues"][0]["depth"] is None
    assert body["queues"][0]["recorded_queued"] == 1


def test_a_hung_broker_cannot_hang_the_request():
    client, _, celery_app = _app()
    release = threading.Event()
    conn = MagicMock()
    conn.connect.side_effect = lambda: release.wait(5)
    try:
        with patch.object(celery_app, "connection_for_read", return_value=conn), \
                patch.object(queues_mod, "BROKER_DEADLINE", 0.2):
            body = client.get("/api/queues").json()
    finally:
        release.set()
    assert "did not answer" in body["broker"]["error"]
    assert body["queues"][0]["depth"] is None


def test_worker_queues_are_cached_between_refreshes():
    client, _, celery_app = _app()
    inspector = MagicMock()
    inspector.active_queues.return_value = {"w1": [{"name": "celery"}]}
    with patch.object(celery_app, "connection_for_read", side_effect=lambda **kw: _fake_conn({"celery": (0, 1)})), \
            patch.object(celery_app.control, "inspect", return_value=inspector) as inspect:
        client.get("/api/queues")
        client.get("/api/queues")
    assert inspect.call_count == 1


# --- purge -------------------------------------------------------------------


def test_purge_requires_auth():
    config = CeleryManagerConfig(celery_app=Celery("t"), db_url="sqlite:///:memory:", secret_key="s")
    app, _ = create_app(config)
    assert TestClient(app, base_url="https://testserver").post("/api/queues/celery/purge").status_code == 401


def test_purge_removes_messages_marks_queued_runs_and_audits():
    eta = datetime(2030, 1, 1, tzinfo=timezone.utc)
    client, session_factory, celery_app = _app(runs=[
        _queued("q1", "slow"), _queued("q2", "slow"),
        _queued("e1", "slow", eta=eta),          # held by a worker, not purged
        _queued("o1", "fast"),                   # other queue
        _queued("s1", "slow", status="started"), # already running
    ])
    conn = _fake_conn({}, purge_result=3)
    with patch.object(celery_app, "connection_for_read", return_value=conn):
        response = client.post("/api/queues/slow/purge")

    assert response.status_code == 200
    assert response.json() == {"queue": "slow", "messages_removed": 3, "runs_marked_revoked": 2}
    conn.channels[0].queue_purge.assert_called_once_with("slow")
    with session_factory() as session:
        status = {r.task_id: r.status for r in session.query(TaskRun).all()}
        assert status == {"q1": "revoked", "q2": "revoked", "e1": "queued", "o1": "queued", "s1": "started"}
        assert session.query(TaskRun).filter_by(task_id="q1").one().finished_at is not None
        audit = session.query(AuditLog).filter_by(action="purge").one()
        assert audit.username == "alice"
        assert (audit.target_type, audit.target_id) == ("queue", "slow")
        details = json.loads(audit.details)
        assert details["messages_removed"] == 3
        assert details["runs_marked_revoked"] == 2
        assert sorted(details["task_ids"]) == ["q1", "q2"]


def test_purge_that_removed_nothing_marks_nothing():
    client, session_factory, celery_app = _app(runs=[_queued("q1", "slow")])
    with patch.object(celery_app, "connection_for_read", return_value=_fake_conn({}, purge_result=0)):
        response = client.post("/api/queues/slow/purge")
    assert response.json()["runs_marked_revoked"] == 0
    with session_factory() as session:
        assert session.query(TaskRun).one().status == "queued"
        assert json.loads(session.query(AuditLog).filter_by(action="purge").one().details)["messages_removed"] == 0


def test_purge_of_a_queue_the_broker_lacks_is_404_and_unaudited():
    client, session_factory, celery_app = _app()
    conn = _fake_conn({}, purge_error=NotFoundError("NOT_FOUND - no queue 'nope'"))
    with patch.object(celery_app, "connection_for_read", return_value=conn):
        response = client.post("/api/queues/nope/purge")
    assert response.status_code == 404
    with session_factory() as session:
        assert session.query(AuditLog).filter_by(action="purge").count() == 0


def test_purge_on_memory_broker_is_503_and_unaudited():
    client, session_factory, _ = _app(broker="memory://", runs=[_queued("q1", "celery")])
    response = client.post("/api/queues/celery/purge")
    assert response.status_code == 503
    assert "in-memory" in response.json()["detail"]
    with session_factory() as session:
        assert session.query(TaskRun).one().status == "queued"
        assert session.query(AuditLog).filter_by(action="purge").count() == 0


def test_purge_with_unreachable_broker_is_503():
    client, _, celery_app = _app()
    conn = MagicMock()
    conn.connect.side_effect = ConnectionRefusedError("refused")
    with patch.object(celery_app, "connection_for_read", return_value=conn):
        response = client.post("/api/queues/celery/purge")
    assert response.status_code == 503
    assert "refused" in response.json()["detail"]


# --- bulk cancel -------------------------------------------------------------


def _bulk_runs():
    return [
        _queued("a", "celery"),
        _queued("b", "celery", status="started"),
        _queued("c", "celery", status="succeeded"),
        _queued("d", "celery", status="failed"),
        _queued("e", "celery", status="retried", task_name="other.task"),
    ]


def test_bulk_cancel_requires_auth():
    config = CeleryManagerConfig(celery_app=Celery("t"), db_url="sqlite:///:memory:", secret_key="s")
    app, _ = create_app(config)
    response = TestClient(app, base_url="https://testserver").post("/api/tasks/cancel-bulk", json={"task_ids": ["a"]})
    assert response.status_code == 401


def test_bulk_cancel_by_ids_revokes_live_runs_and_reports_skips():
    client, session_factory, celery_app = _app(runs=_bulk_runs())
    with patch.object(celery_app.control, "revoke") as revoke:
        response = client.post("/api/tasks/cancel-bulk", json={
            "task_ids": ["a", "b", "c", "d", "zzz", "a"], "terminate": True,
        })
    assert response.status_code == 200
    body = response.json()
    assert body["revoked"] == 2
    assert body["skipped_finished"] == 2
    assert body["unknown"] == 1
    revoke.assert_called_once_with(["a", "b"], terminate=True)
    with session_factory() as session:
        audit = session.query(AuditLog).filter_by(action="cancel_bulk").one()
        details = json.loads(audit.details)
        assert audit.target_id == "selection"
        assert details["task_ids"] == ["a", "b"]
        assert details["terminate"] is True
        assert details["skipped_finished"] == 2


def test_bulk_cancel_by_filter_uses_the_list_filters():
    client, session_factory, celery_app = _app(runs=_bulk_runs())
    with patch.object(celery_app.control, "revoke") as revoke:
        body = client.post("/api/tasks/cancel-bulk", json={"filter": {"task_name": "demo.echo"}}).json()
    assert body["revoked"] == 2  # a (queued), b (started); e is another task
    revoke.assert_called_once_with(["a", "b"], terminate=False)
    with session_factory() as session:
        details = json.loads(session.query(AuditLog).filter_by(action="cancel_bulk").one().details)
        assert details["filter"] == {"task_name": "demo.echo"}


def test_bulk_cancel_filter_with_a_finished_status_matches_nothing():
    client, session_factory, celery_app = _app(runs=_bulk_runs())
    with patch.object(celery_app.control, "revoke") as revoke:
        body = client.post("/api/tasks/cancel-bulk", json={"filter": {"status": "failed"}}).json()
    assert body["revoked"] == 0
    revoke.assert_not_called()
    with session_factory() as session:
        assert session.query(AuditLog).filter_by(action="cancel_bulk").count() == 0


def test_bulk_cancel_dry_run_counts_without_revoking_or_auditing():
    client, session_factory, celery_app = _app(runs=_bulk_runs())
    with patch.object(celery_app.control, "revoke") as revoke:
        body = client.post("/api/tasks/cancel-bulk", json={"filter": {}, "dry_run": True}).json()
    assert body["matched"] == 3
    assert body["revoked"] == 0
    assert body["dry_run"] is True
    revoke.assert_not_called()
    with session_factory() as session:
        assert session.query(AuditLog).count() == 0


def test_bulk_cancel_over_the_cap_is_refused_whole():
    client, session_factory, celery_app = _app(runs=_bulk_runs())
    with patch("celery_manager.control.tasks.MAX_BULK_CANCEL", 2), \
            patch.object(celery_app.control, "revoke") as revoke:
        by_filter = client.post("/api/tasks/cancel-bulk", json={"filter": {}})
        by_ids = client.post("/api/tasks/cancel-bulk", json={"task_ids": ["a", "b", "c"]})
    assert by_filter.status_code == 400
    assert "3 runs match" in by_filter.json()["detail"]
    assert by_ids.status_code == 400
    revoke.assert_not_called()
    with session_factory() as session:
        assert session.query(AuditLog).count() == 0


def test_bulk_cancel_revokes_in_chunks():
    runs = [_queued(f"t{i}", "celery") for i in range(5)]
    client, _, celery_app = _app(runs=runs)
    with patch("celery_manager.control.tasks._REVOKE_CHUNK", 2), \
            patch.object(celery_app.control, "revoke") as revoke:
        body = client.post("/api/tasks/cancel-bulk", json={"filter": {"status": "queued"}}).json()
    assert body["revoked"] == 5
    assert [c.args[0] for c in revoke.call_args_list] == [["t0", "t1"], ["t2", "t3"], ["t4"]]


@pytest.mark.parametrize("payload", [{}, {"task_ids": ["a"], "filter": {}}])
def test_bulk_cancel_needs_exactly_one_selector(payload):
    client, _, celery_app = _app(runs=_bulk_runs())
    with patch.object(celery_app.control, "revoke") as revoke:
        response = client.post("/api/tasks/cancel-bulk", json=payload)
    assert response.status_code == 400
    revoke.assert_not_called()
