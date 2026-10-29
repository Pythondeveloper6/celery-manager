from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import AuditLog, TaskRun


def registered_placeholder(*args, **kwargs):
    return None


def _client_and_session():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    config.celery_app.task(name="demo.echo", shared=False)(registered_placeholder)
    config.celery_app.task(name="demo.plain", shared=False)(registered_placeholder)
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(TaskRun(task_id="t1", task_name="demo.echo", status="failed", args="[]", kwargs="{}"))
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory


def test_list_tasks_requires_auth():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    response = TestClient(app, base_url="https://testserver").get("/api/tasks")
    assert response.status_code == 401


def test_list_tasks_returns_seeded_run():
    client, _ = _client_and_session()
    response = client.get("/api/tasks")
    assert response.status_code == 200
    body = response.json()
    # Envelope, not a bare array (Task 19): the array had nowhere to put the
    # filtered total, so the UI could not tell "1 result" from "the first
    # page of many".
    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    assert body["items"][0]["task_id"] == "t1"


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


# --- I2: audit coverage. `audit.record` used to be called only from
# retry_task, so start and cancel — both destructive operator actions taken
# through the dashboard's own API — left no trace of who did what. ---


def test_start_task_endpoint_writes_an_audit_row():
    client, session_factory = _client_and_session()
    with patch("celery_manager.control.tasks.Celery.send_task", return_value=MagicMock(id="new-1"), create=True):
        response = client.post("/api/tasks/start", json={"task_name": "demo.echo", "args": [1], "kwargs": {}})

    assert response.status_code == 200
    assert response.json() == {"task_id": "new-1"}

    with session_factory() as session:
        rows = session.query(AuditLog).filter_by(action="start").all()
        assert len(rows) == 1
        assert rows[0].username == "alice"
        assert rows[0].target_type == "task"
        assert rows[0].target_id == "new-1"
        assert "demo.echo" in rows[0].details


def test_cancel_task_endpoint_writes_an_audit_row():
    client, session_factory = _client_and_session()
    with patch("celery_manager.control.tasks.Celery.control", create=True):
        response = client.post("/api/tasks/t1/cancel?terminate=true")

    assert response.status_code == 200

    with session_factory() as session:
        rows = session.query(AuditLog).filter_by(action="cancel").all()
        assert len(rows) == 1
        assert rows[0].username == "alice"
        assert rows[0].target_type == "task"
        assert rows[0].target_id == "t1"
        assert "true" in rows[0].details.lower()


def test_retry_does_not_double_log_as_both_retry_and_start():
    """`retry_task` dispatches via `start_task`; auditing unconditionally
    inside `start_task` would record every retry twice under two names."""
    client, session_factory = _client_and_session()
    with patch("celery_manager.control.tasks.Celery.send_task", return_value=MagicMock(id="t1-retry"), create=True):
        response = client.post("/api/tasks/t1/retry")

    assert response.status_code == 200
    with session_factory() as session:
        actions = [r.action for r in session.query(AuditLog).all()]
        assert actions == ["retry"]


def test_task_detail_includes_worker_queue_and_timing_fields():
    """The detail endpoint stored these on TaskRun but never returned them,
    which also made the frontend's TaskDetail type (extending TaskSummary)
    claim started_at/finished_at that never arrived."""
    from datetime import datetime, timezone

    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    config.celery_app.task(name="demo.echo", shared=False)(registered_placeholder)
    config.celery_app.task(name="demo.plain", shared=False)(registered_placeholder)
    app, session_factory = create_app(config)
    started = datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc)
    finished = datetime(2026, 8, 15, 6, 0, 3, tzinfo=timezone.utc)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(TaskRun(
            task_id="t9", task_name="demo.echo", status="succeeded", args="[]", kwargs="{}",
            queue="reports", worker="worker-1@host", started_at=started, finished_at=finished,
            runtime_seconds=3.25,
        ))
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})

    body = client.get("/api/tasks/t9").json()

    assert body["queue"] == "reports"
    assert body["worker"] == "worker-1@host"
    assert body["runtime_seconds"] == 3.25
    assert body["started_at"] is not None and "2026-08-15" in body["started_at"]
    assert body["finished_at"] is not None and "2026-08-15" in body["finished_at"]


def test_task_detail_tolerates_missing_worker_queue_and_timings():
    """A queued-but-never-started task must still return every key, as null."""
    client, _ = _client_and_session()
    body = client.get("/api/tasks/t1").json()
    for field in ("queue", "worker", "started_at", "finished_at", "runtime_seconds"):
        assert field in body, field
        assert body[field] is None, field


# --- Task 19: search, date-range filtering, paging, retry lineage, and the
# UTC-offset fix on every emitted timestamp. ---


def _client_with_history():
    """Four runs across two task names and two eras, one of them carrying
    progress and timing values, one of them a retry of another."""
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    config.celery_app.task(name="demo.echo", shared=False)(registered_placeholder)
    config.celery_app.task(name="demo.plain", shared=False)(registered_placeholder)
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add_all([
            TaskRun(task_id="old-1", task_name="demo.echo", status="succeeded", args="[]", kwargs="{}",
                    started_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            TaskRun(task_id="new-2", task_name="demo.send_report", status="succeeded",
                    args='["100%"]', kwargs='{"user": "alice"}',
                    started_at=datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 8, 15, 6, 0, 3, tzinfo=timezone.utc),
                    runtime_seconds=3.25,
                    progress_current=7, progress_total=10, progress_message="crunching",
                    progress_updated_at=datetime(2026, 8, 15, 6, 0, 2, tzinfo=timezone.utc)),
            TaskRun(task_id="new-3", task_name="demo.echo", status="failed", args="[]", kwargs="{}",
                    started_at=datetime(2026, 8, 16, tzinfo=timezone.utc)),
            TaskRun(task_id="new-3-retry", task_name="demo.echo", status="succeeded", args="[]", kwargs="{}",
                    started_at=datetime(2026, 8, 17, tzinfo=timezone.utc), retried_from_id="new-3"),
        ])
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory


def test_list_tasks_search_reaches_the_control_layer():
    client, _ = _client_with_history()
    body = client.get("/api/tasks", params={"q": "send_report"}).json()
    assert [t["task_id"] for t in body["items"]] == ["new-2"]
    assert body["total"] == 1


def test_list_tasks_search_matches_kwargs_content():
    client, _ = _client_with_history()
    body = client.get("/api/tasks", params={"q": "alice"}).json()
    assert [t["task_id"] for t in body["items"]] == ["new-2"]


def test_list_tasks_search_does_not_treat_percent_as_a_wildcard():
    """The Part-1 escaping guard, at the HTTP boundary: `?q=%` must find the
    one run whose args contain a literal percent, not the whole table."""
    client, _ = _client_with_history()
    body = client.get("/api/tasks", params={"q": "%"}).json()
    assert [t["task_id"] for t in body["items"]] == ["new-2"]
    assert body["total"] == 1


def test_list_tasks_filters_by_date_range():
    client, _ = _client_with_history()
    body = client.get("/api/tasks", params={"date_from": "2025-01-01T00:00:00Z"}).json()
    assert "old-1" not in [t["task_id"] for t in body["items"]]
    assert body["total"] == 3

    windowed = client.get("/api/tasks", params={
        "date_from": "2026-08-15T00:00:00Z", "date_to": "2026-08-16T23:59:59Z",
    }).json()
    assert sorted(t["task_id"] for t in windowed["items"]) == ["new-2", "new-3"]


def test_list_tasks_combines_search_and_date_range():
    client, _ = _client_with_history()
    body = client.get("/api/tasks", params={"q": "echo", "date_from": "2026-08-17T00:00:00Z"}).json()
    assert [t["task_id"] for t in body["items"]] == ["new-3-retry"]
    assert body["total"] == 1


def test_list_tasks_total_reflects_the_filtered_count_not_the_table_size():
    """"Showing 1 of 4" would be a lie if `total` counted the table. The
    control layer shares one filter builder between list and count precisely
    so this can't drift."""
    client, _ = _client_with_history()
    body = client.get("/api/tasks", params={"status": "failed"}).json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_list_tasks_paging_returns_disjoint_pages_over_a_stable_total():
    client, _ = _client_with_history()
    page_1 = client.get("/api/tasks", params={"limit": 2, "offset": 0}).json()
    page_2 = client.get("/api/tasks", params={"limit": 2, "offset": 2}).json()

    ids_1 = [t["task_id"] for t in page_1["items"]]
    ids_2 = [t["task_id"] for t in page_2["items"]]
    assert len(ids_1) == 2 and len(ids_2) == 2
    assert set(ids_1).isdisjoint(ids_2)
    assert sorted(ids_1 + ids_2) == ["new-2", "new-3", "new-3-retry", "old-1"]
    assert page_1["total"] == page_2["total"] == 4
    assert page_1["offset"] == 0 and page_2["offset"] == 2


def test_list_tasks_clamps_an_absurd_limit():
    """A hand-crafted `?limit=10000000` must not pull the whole table into
    one response. Clamped, not rejected — the caller still gets a usable
    page, and the echoed `limit` tells them what they actually got."""
    client, _ = _client_with_history()
    body = client.get("/api/tasks", params={"limit": 10_000_000}).json()
    assert body["limit"] == 500


def test_list_tasks_floors_a_negative_limit_and_offset():
    client, _ = _client_with_history()
    body = client.get("/api/tasks", params={"limit": -5, "offset": -3}).json()
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert len(body["items"]) == 1


def test_list_tasks_items_carry_runtime_and_progress_fields():
    client, _ = _client_with_history()
    items = {t["task_id"]: t for t in client.get("/api/tasks").json()["items"]}

    assert items["new-2"]["runtime_seconds"] == 3.25
    assert items["new-2"]["progress_current"] == 7
    assert items["new-2"]["progress_total"] == 10
    assert items["new-2"]["progress_message"] == "crunching"
    assert items["new-2"]["progress_updated_at"] is not None
    # Progress is opt-in, so a run that never reported any still has every
    # key, as null — the frontend renders "no progress", not 0%.
    for field in ("runtime_seconds", "progress_current", "progress_total",
                  "progress_message", "progress_updated_at"):
        assert field in items["old-1"], field
        assert items["old-1"][field] is None, field


def test_get_task_detail_includes_retry_chain_fields():
    client, _ = _client_with_history()

    parent = client.get("/api/tasks/new-3").json()
    assert parent["retried_from_id"] is None
    assert parent["retried_by"] == ["new-3-retry"]

    child = client.get("/api/tasks/new-3-retry").json()
    assert child["retried_from_id"] == "new-3"
    assert child["retried_by"] == []


def test_get_task_detail_includes_progress_fields():
    client, _ = _client_with_history()
    body = client.get("/api/tasks/new-2").json()
    assert body["progress_current"] == 7
    assert body["progress_total"] == 10
    assert body["progress_message"] == "crunching"
    assert body["progress_updated_at"] is not None


def test_retry_task_endpoint_stamps_the_retry_chain():
    client, _ = _client_with_history()
    with patch("celery_manager.control.tasks.Celery.send_task", return_value=MagicMock(id="new-3-retry-2"), create=True):
        response = client.post("/api/tasks/new-3/retry")

    assert response.status_code == 200
    new_id = response.json()["new_task_id"]

    assert client.get(f"/api/tasks/{new_id}").json()["retried_from_id"] == "new-3"
    assert client.get("/api/tasks/new-3").json()["retried_by"] == ["new-3-retry", new_id]


def _is_utc_marked(value: str) -> bool:
    return value.endswith(("Z", "+00:00"))


def test_emitted_datetimes_carry_a_utc_offset_on_the_list_endpoint():
    """Part 4 regression guard. SQLite drops tzinfo on round-trip, so an
    unpatched response serializes "2026-08-15T06:00:00" — which a browser's
    `new Date()` reads as *local* time, skewing every timestamp (and any
    elapsed-time counter) by the viewer's UTC offset."""
    client, _ = _client_with_history()
    item = next(t for t in client.get("/api/tasks").json()["items"] if t["task_id"] == "new-2")

    for field in ("started_at", "finished_at", "progress_updated_at"):
        assert _is_utc_marked(item[field]), (field, item[field])


def test_emitted_datetimes_carry_a_utc_offset_on_the_detail_endpoint():
    client, _ = _client_with_history()
    body = client.get("/api/tasks/new-2").json()

    for field in ("started_at", "finished_at", "progress_updated_at"):
        assert _is_utc_marked(body[field]), (field, body[field])


def test_emitted_datetimes_parse_back_to_the_exact_instant_stored():
    """Not just "has an offset" — the offset must be the *right* one, i.e.
    the value must round-trip to the UTC instant that was written."""
    client, _ = _client_with_history()
    body = client.get("/api/tasks/new-2").json()

    parsed = datetime.fromisoformat(body["started_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc)


def test_eta_is_emitted_with_a_utc_offset_too():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    config.celery_app.task(name="demo.echo", shared=False)(registered_placeholder)
    config.celery_app.task(name="demo.plain", shared=False)(registered_placeholder)
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(TaskRun(
            task_id="scheduled-1", task_name="demo.echo", status="queued", args="[]", kwargs="{}",
            eta=datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc),
        ))
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})

    body = client.get("/api/tasks/scheduled-1").json()
    assert _is_utc_marked(body["eta"])


def test_null_datetimes_stay_null():
    """The UTC helper must not invent a timestamp for a run that has none."""
    client, _ = _client_and_session()
    body = client.get("/api/tasks/t1").json()
    assert body["started_at"] is None
    assert body["finished_at"] is None
    assert body["eta"] is None
    assert body["progress_updated_at"] is None


# --- Status codes and inbound timestamps at the HTTP boundary ---


def test_retry_of_an_unknown_task_id_is_404_not_500():
    """`GET /api/tasks/{id}` already answers 404 for an id it has never seen;
    the retry route used to let the same condition escape as an unhandled
    ValueError, i.e. a 500 — indistinguishable from a real server fault to
    anyone reading logs or alerting on 5xx."""
    client, _ = _client_and_session()
    response = client.post("/api/tasks/does-not-exist/retry")
    assert response.status_code == 404


def test_bulk_retry_with_an_unknown_task_id_is_404_not_500():
    """`bulk_retry` stops at the first unknown id rather than reporting
    per-item results (a documented limitation), so a batch containing one is
    reported the same way a single unknown id is."""
    client, _ = _client_and_session()
    with patch("celery_manager.control.tasks.Celery.send_task", return_value=MagicMock(id="t1-retry"), create=True):
        response = client.post("/api/tasks/retry-bulk", json={"task_ids": ["t1", "does-not-exist"]})
    assert response.status_code == 404


def test_retry_of_a_run_only_the_events_listener_recorded():
    """An undecorated task's row is written by the listener, whose `args` are
    a Python repr rather than JSON. Decoding them with `json.loads` alone
    made the dashboard's retry button 500 for every such run."""
    from celery_manager.events.listener import upsert_task_run

    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    config.celery_app.task(name="demo.echo", shared=False)(registered_placeholder)
    config.celery_app.task(name="demo.plain", shared=False)(registered_placeholder)
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        upsert_task_run(session, {
            "type": "task-received", "uuid": "listener-only", "name": "demo.plain",
            "args": "(5,)", "kwargs": "{'b': 2}",
        })
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})

    with patch("celery_manager.control.tasks.Celery.send_task", return_value=MagicMock(id="listener-only-retry"), create=True) as send:
        response = client.post("/api/tasks/listener-only/retry")

    assert response.status_code == 200
    send.assert_called_once_with("demo.plain", args=[5], kwargs={"b": 2}, headers={"cm_initiator": "alice"})


def test_date_bounds_with_a_non_utc_offset_name_the_same_instant():
    """SQLAlchemy's SQLite DATETIME binds a datetime by formatting its fields
    and dropping tzinfo, so `?date_from=...+02:00` was compared as local wall
    time and shifted the window by the caller's offset. The three bounds below
    are the same instant and must return the same rows."""
    client, _ = _client_with_history()

    as_utc = client.get("/api/tasks", params={"date_from": "2026-08-15T12:00:00Z"}).json()
    as_plus_two = client.get("/api/tasks", params={"date_from": "2026-08-15T14:00:00+02:00"}).json()
    as_minus_five = client.get("/api/tasks", params={"date_from": "2026-08-15T07:00:00-05:00"}).json()

    assert as_utc["total"] == as_plus_two["total"] == as_minus_five["total"]
    assert (
        [t["task_id"] for t in as_utc["items"]]
        == [t["task_id"] for t in as_plus_two["items"]]
        == [t["task_id"] for t in as_minus_five["items"]]
    )
    # And the window is the real one: new-2 started at 06:00Z, before the
    # bound, so only the two later runs qualify.
    assert sorted(t["task_id"] for t in as_utc["items"]) == ["new-3", "new-3-retry"]
