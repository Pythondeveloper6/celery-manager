"""Dead-letter view: which runs count, filtering, and re-running them."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.deadletter import (
    MAX_BULK_RERUN,
    BulkRerunTooLarge,
    MixedTaskNames,
    NotDeadLetter,
    earlier_attempts,
    list_dead_letters,
    rerun_with_arguments,
    summarize_dead_letters,
)
from celery_manager.control.tasks import TaskRunNotFound
from celery_manager.db.models import AuditLog, Base, TaskRun

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def registered_placeholder(*args, **kwargs):
    return None


def _run(task_id, status="failed", name="demo.pay", exception="ConnectionError('boom')", **extra):
    fields = {
        "task_id": task_id, "task_name": name, "status": status, "exception": exception,
        "args": '["a"]', "kwargs": '{"k": 1}', "finished_at": NOW, "started_at": NOW,
    }
    fields.update(extra)
    return TaskRun(**fields)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def celery_app():
    app = MagicMock(spec=Celery)
    counter = iter(range(1, 10_000))
    app.send_task.side_effect = lambda *a, **k: MagicMock(id=f"new-{next(counter)}")
    return app


def _ids(session, **kw):
    rows, total = list_dead_letters(session, **kw)
    return [r.task_id for r in rows], total


# --- definition ---------------------------------------------------------


def test_only_failed_runs_at_the_end_of_their_chain_are_dead_letters(session):
    session.add_all([
        _run("dead"),
        _run("ok", status="succeeded"),
        _run("celery-retrying", status="retried"),
        _run("queued", status="queued"),
        _run("revoked", status="revoked"),
        # An operator retried this one; the retry points back at it.
        _run("was-retried"),
        _run("retry-of-it", status="succeeded", retried_from_id="was-retried"),
        # A failed retry is itself a dead letter; its parent is not.
        _run("first"),
        _run("second", retried_from_id="first"),
    ])
    session.commit()

    ids, total = _ids(session)

    assert sorted(ids) == ["dead", "second"]
    assert total == 2


def test_a_retry_that_is_still_queued_already_clears_the_original(session):
    session.add_all([_run("a"), _run("a2", status="queued", retried_from_id="a")])
    session.commit()
    assert _ids(session) == ([], 0)


# --- filters and pagination --------------------------------------------


def test_filters_by_task_exception_type_and_since(session):
    old = NOW - timedelta(days=3)
    session.add_all([
        _run("pay-conn", name="demo.pay", exception="ConnectionError('x')"),
        _run("pay-val", name="demo.pay", exception="ValueError: bad"),
        _run("mail-conn", name="demo.mail", exception="ConnectionError('y')"),
        _run("old", name="demo.mail", exception="ValueError('z')", finished_at=old, started_at=old),
    ])
    session.commit()

    assert _ids(session, task_name="demo.mail")[1] == 2
    assert sorted(_ids(session, exception="ConnectionError")[0]) == ["mail-conn", "pay-conn"]
    assert _ids(session, exception="ValueError", task_name="demo.pay")[0] == ["pay-val"]
    ids, total = _ids(session, since=NOW - timedelta(days=1))
    assert "old" not in ids and total == 3


def test_pagination_returns_a_page_and_the_full_total(session):
    session.add_all([_run(f"t{i}") for i in range(7)])
    session.commit()

    page, total = _ids(session, limit=3, offset=3)
    assert total == 7
    assert page == ["t3", "t2", "t1"]  # newest first

    page, total = _ids(session, exception="ConnectionError", limit=2, offset=6)
    assert (page, total) == (["t0"], 7)


def test_summary_groups_by_task_and_exception_type(session):
    session.add_all([
        _run("a", name="demo.pay", exception="ConnectionError('1')"),
        _run("b", name="demo.pay", exception="ConnectionError('2')"),
        _run("c", name="demo.pay", exception="ValueError('3')"),
        _run("d", name="demo.mail", exception="ConnectionError('4')"),
        _run("fine", status="succeeded"),
    ])
    session.commit()

    summary = summarize_dead_letters(session)

    assert summary["total"] == 4 and summary["truncated"] is False
    assert summary["groups"][0] == {"task_name": "demo.pay", "exception_type": "ConnectionError", "count": 2}
    assert len(summary["groups"]) == 3


def test_earlier_attempts_counts_the_chain(session):
    session.add_all([
        _run("r0", status="failed"),
        _run("r1", retried_from_id="r0"),
        _run("r2", retried_from_id="r1"),
        _run("solo"),
    ])
    session.commit()
    runs, _ = list_dead_letters(session)
    assert earlier_attempts(session, runs) == {"r2": 2, "solo": 0}


# --- re-running ---------------------------------------------------------


def test_rerun_with_same_arguments_links_the_chain_and_leaves_the_list(session, celery_app):
    session.add(_run("d1", queue="payments"))
    session.commit()

    result = rerun_with_arguments(celery_app, session, ["d1"], None, None, "alice")

    celery_app.send_task.assert_called_once_with("demo.pay", args=["a"], kwargs={"k": 1}, queue="payments")
    new_id = result["reruns"][0]["new_task_id"]
    new_run = session.query(TaskRun).filter_by(task_id=new_id).one()
    assert new_run.retried_from_id == "d1"
    assert new_run.queue == "payments" and new_run.status == "queued"
    assert _ids(session) == ([], 0)
    audit = session.query(AuditLog).one()
    assert (audit.action, audit.target_id, audit.username) == ("rerun_dead_letter", "d1", "alice")
    details = json.loads(audit.details)
    assert details["new_task_id"] == new_id and details["args_edited"] is False


def test_rerun_with_edited_arguments_sends_them_and_audits_the_change(session, celery_app):
    session.add(_run("d1"))
    session.commit()

    rerun_with_arguments(celery_app, session, ["d1"], ["fixed"], {"k": 2}, "alice")

    celery_app.send_task.assert_called_once_with("demo.pay", args=["fixed"], kwargs={"k": 2})
    details = json.loads(session.query(AuditLog).one().details)
    assert details["args_edited"] is True
    assert details["args"] == ["fixed"] and details["kwargs"] == {"k": 2}
    new = session.query(TaskRun).filter_by(retried_from_id="d1").one()
    assert json.loads(new.args) == ["fixed"]


def test_arguments_equal_to_the_stored_ones_are_not_audited_as_edited(session, celery_app):
    session.add(_run("d1"))
    session.commit()
    rerun_with_arguments(celery_app, session, ["d1"], ["a"], {"k": 1}, "alice")
    assert json.loads(session.query(AuditLog).one().details)["args_edited"] is False


def test_a_failed_rerun_becomes_a_new_dead_letter_with_history(session, celery_app):
    session.add(_run("d1"))
    session.commit()
    new_id = rerun_with_arguments(celery_app, session, ["d1"], None, None, "alice")["reruns"][0]["new_task_id"]

    session.query(TaskRun).filter_by(task_id=new_id).update(
        {"status": "failed", "exception": "ConnectionError('again')"}
    )
    session.commit()

    rows, total = list_dead_letters(session)
    assert [r.task_id for r in rows] == [new_id] and total == 1
    assert earlier_attempts(session, rows) == {new_id: 1}


def test_bulk_rerun_same_arguments_for_mixed_tasks(session, celery_app):
    session.add_all([_run("a", name="demo.pay"), _run("b", name="demo.mail", args='["m"]')])
    session.commit()

    result = rerun_with_arguments(celery_app, session, ["a", "b", "a"], None, None, "alice")

    assert [r["task_id"] for r in result["reruns"]] == ["a", "b"]  # de-duplicated
    assert session.query(AuditLog).count() == 2
    assert _ids(session) == ([], 0)
    assert json.loads(session.query(AuditLog).first().details)["bulk"] is True


def test_bulk_rerun_with_edited_arguments_for_one_task(session, celery_app):
    session.add_all([_run("a"), _run("b")])
    session.commit()

    rerun_with_arguments(celery_app, session, ["a", "b"], ["x"], {}, "alice")

    sent = [c.kwargs["args"] for c in celery_app.send_task.call_args_list]
    assert sent == [["x"], ["x"]]


def test_edited_arguments_across_different_tasks_are_refused_before_dispatch(session, celery_app):
    session.add_all([_run("a", name="demo.pay"), _run("b", name="demo.mail")])
    session.commit()

    with pytest.raises(MixedTaskNames):
        rerun_with_arguments(celery_app, session, ["a", "b"], ["x"], None, "alice")

    celery_app.send_task.assert_not_called()
    assert session.query(AuditLog).count() == 0
    assert _ids(session)[1] == 2


def test_refuses_runs_that_are_not_dead_letters_without_dispatching(session, celery_app):
    session.add_all([_run("dead"), _run("ok", status="succeeded"), _run("done"), _run("c", retried_from_id="done")])
    session.commit()

    with pytest.raises(NotDeadLetter) as err:
        rerun_with_arguments(celery_app, session, ["dead", "ok", "done"], None, None, "alice")

    assert err.value.task_ids == ["ok", "done"]
    celery_app.send_task.assert_not_called()


def test_unknown_id_and_oversized_selection_are_refused(session, celery_app):
    with pytest.raises(TaskRunNotFound):
        rerun_with_arguments(celery_app, session, ["nope"], None, None, "alice")
    with pytest.raises(BulkRerunTooLarge):
        rerun_with_arguments(celery_app, session, [f"t{i}" for i in range(MAX_BULK_RERUN + 1)], None, None, "alice")
    celery_app.send_task.assert_not_called()


def test_undecodable_stored_arguments_refuse_the_whole_request(session, celery_app):
    session.add_all([_run("good"), _run("bad", args="<truncated")])
    session.commit()

    with pytest.raises(ValueError, match="could not decode"):
        rerun_with_arguments(celery_app, session, ["good", "bad"], None, None, "alice")

    celery_app.send_task.assert_not_called()
    # Supplying the arguments explicitly is the way out.
    rerun_with_arguments(celery_app, session, ["bad"], ["ok"], None, "alice")
    celery_app.send_task.assert_called_once()


def test_a_broker_error_on_one_run_is_reported_and_the_rest_still_go(session, celery_app):
    session.add_all([_run("a"), _run("b")])
    session.commit()
    ok = celery_app.send_task.side_effect
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("broker down")
        return ok(*a, **k)

    celery_app.send_task.side_effect = flaky

    result = rerun_with_arguments(celery_app, session, ["a", "b"], None, None, "alice")

    assert [e["task_id"] for e in result["errors"]] == ["a"]
    assert [r["task_id"] for r in result["reruns"]] == ["b"]
    assert _ids(session) == (["a"], 1)  # the failed send leaves it listed


# --- HTTP ---------------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    celery = Celery("dl")
    celery.task(name="demo.pay", shared=False)(registered_placeholder)
    celery.task(name="demo.mail", shared=False)(registered_placeholder)
    config = CeleryManagerConfig(celery_app=celery, db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as s:
        create_user(s, "viewer", "pw", role="viewer")
        create_user(s, "op", "pw", role="operator")
        s.add_all([_run("d1"), _run("d2"), _run("d3", name="demo.mail", traceback="Traceback...\nValueError: nope\n")])
        s.commit()
    send = MagicMock(side_effect=lambda *a, **k: MagicMock(id=f"n{len(send.call_args_list)}"))
    monkeypatch.setattr("celery_manager.control.tasks.Celery.send_task", send)

    def client(name):
        c = TestClient(app, base_url="https://testserver")
        assert c.post("/api/auth/login", json={"username": name, "password": "pw"}).status_code == 200
        return c

    return client, session_factory, send


def test_api_lists_dead_letters_with_summary_for_any_signed_in_user(api):
    client, _, _ = api
    body = client("viewer").get("/api/dead-letters").json()
    assert body["total"] == 3 and body["limit"] == 50 and body["offset"] == 0
    mail = next(i for i in body["items"] if i["task_id"] == "d3")
    assert mail["last_line"] == "ValueError: nope"
    assert mail["exception_type"] == "ConnectionError"
    assert {g["task_name"] for g in body["summary"]["groups"]} == {"demo.pay", "demo.mail"}
    assert client("viewer").get("/api/dead-letters?task_name=demo.pay&limit=1").json()["total"] == 2


def test_api_requires_sign_in(api):
    client, _, _ = api
    anon = TestClient(client("op").app, base_url="https://testserver")
    assert anon.get("/api/dead-letters").status_code == 401


def test_viewer_cannot_rerun(api):
    client, _, send = api
    response = client("viewer").post("/api/dead-letters/rerun", json={"task_ids": ["d1"]})
    assert response.status_code == 403
    send.assert_not_called()


def test_operator_reruns_and_the_run_leaves_the_list(api):
    client, _, send = api
    op = client("op")
    response = op.post("/api/dead-letters/rerun", json={"task_ids": ["d1", "d2"]})
    assert response.status_code == 200
    assert len(response.json()["reruns"]) == 2
    assert op.get("/api/dead-letters").json()["total"] == 1
    # Running it again is a conflict, not a duplicate dispatch.
    again = op.post("/api/dead-letters/rerun", json={"task_ids": ["d1"]})
    assert again.status_code == 409
    assert send.call_count == 2


def test_api_error_statuses(api):
    client, _, send = api
    op = client("op")
    post = lambda body: op.post("/api/dead-letters/rerun", json=body)
    assert post({"task_ids": ["d1"], "args": {"a": 1}}).status_code == 422
    assert post({"task_ids": ["d1"], "kwargs": ["a"]}).status_code == 422
    assert post({"task_ids": []}).status_code == 422
    assert post({"task_ids": ["nope"]}).status_code == 404
    mixed = post({"task_ids": ["d1", "d3"], "args": ["x"]})
    assert mixed.status_code == 400 and "single task" in mixed.json()["detail"]
    assert post({"task_ids": [f"t{i}" for i in range(MAX_BULK_RERUN + 1)]}).status_code == 400
    send.assert_not_called()
