# tests/unit/test_live_updates.py
#
# Live dashboard updates across processes. The writers that matter (the
# events listener, workers) run in other processes than the API, so the API
# learns about their writes by polling `task_runs.updated_at`. Two halves:
#
#   * every write path bumps `updated_at` — a writer that forgets is a run
#     the dashboard silently stops updating, and
#   * the API-side change feed turns those bumps into stream events, without
#     missing rows that share a timestamp or commit late, and only while
#     somebody is subscribed.
#
# "Another process" is modelled as a separate engine on the same SQLite
# file: nothing but the database is shared, exactly as in production.

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from alembic import command
from celery import Celery
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from celery_manager import decorator as decorator_module
from celery_manager import progress as progress_module
from celery_manager import report_progress
from celery_manager.api.app import create_app
from celery_manager.api.sse import EventBus
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.tasks import bulk_retry, retry_task, start_task
from celery_manager.db.changefeed import RunChangeFeed
from celery_manager.db.migrate import alembic_config
from celery_manager.db.models import Base, TaskRun
from celery_manager.decorator import track
from celery_manager.events.listener import upsert_task_run

LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _factory(db_url: str):
    """A fresh engine + session factory on `db_url` — one per simulated
    process."""
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'live.db'}"


def _seed(Session, task_id: str, updated_at: datetime = LONG_AGO, **fields) -> None:
    with Session() as session:
        session.add(TaskRun(
            task_id=task_id,
            task_name=fields.pop("task_name", "demo.echo"),
            status=fields.pop("status", "queued"),
            updated_at=updated_at,
            **fields,
        ))
        session.commit()


def _updated_at(Session, task_id: str) -> datetime:
    with Session() as session:
        value = session.query(TaskRun.updated_at).filter_by(task_id=task_id).scalar()
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _is_recent(value: datetime) -> bool:
    return datetime.now(timezone.utc) - value < timedelta(minutes=1)


# --- every write path bumps updated_at ---------------------------------------


def test_listener_upsert_stamps_a_new_row_and_bumps_an_existing_one(db_url):
    Session = _factory(db_url)
    with Session() as session:
        upsert_task_run(session, {"type": "task-sent", "uuid": "new", "name": "demo.echo"})
        session.commit()
    assert _is_recent(_updated_at(Session, "new"))

    _seed(Session, "old", status="queued")
    with Session() as session:
        upsert_task_run(session, {"type": "task-started", "uuid": "old", "hostname": "w1"})
        session.commit()
    assert _is_recent(_updated_at(Session, "old"))


@pytest.fixture
def worker(db_url, monkeypatch):
    """An eager Celery app whose decorator writes to `db_url`, as a worker
    process configured with `celery_manager.setup(config)` would."""
    Session = _factory(db_url)
    app = Celery("live_updates_test", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    monkeypatch.setattr(decorator_module, "_session_factory", Session)
    monkeypatch.setattr(decorator_module, "_eta_tolerance_seconds", 60)
    monkeypatch.setattr(progress_module, "_warned_no_task_id", False)
    return app, Session


def test_track_decorator_bumps_updated_at_on_an_existing_row(worker):
    app, Session = worker
    _seed(Session, "dec-1", status="queued")

    @app.task(name="live.dec", bind=True)
    @track()
    def dec(self):
        return "ok"

    dec.apply(task_id="dec-1")
    assert _is_recent(_updated_at(Session, "dec-1"))


def test_report_progress_bumps_updated_at(worker):
    app, Session = worker
    stamps = []

    @app.task(name="live.progress", bind=True)
    @track()
    def work(self):
        with Session() as session:
            # Wind the clock back mid-run, so only the progress write can
            # move it forward again.
            session.query(TaskRun).filter_by(task_id="prog-1").update({"updated_at": LONG_AGO})
            session.commit()
        report_progress(1, 2, "half")
        stamps.append(_updated_at(Session, "prog-1"))
        return "ok"

    work.apply(task_id="prog-1")
    assert _is_recent(stamps[0])


def test_start_retry_and_bulk_retry_stamp_their_rows(db_url):
    Session = _factory(db_url)
    celery_app = MagicMock()
    ids = iter(["started-1", "retry-1", "bulk-1"])
    celery_app.send_task.side_effect = lambda *a, **k: MagicMock(id=next(ids))
    _seed(Session, "orig", status="failed", args="[1]", kwargs="{}")

    with Session() as session:
        start_task(celery_app, "demo.echo", [], {}, session=session, username="alice")
        retry_task(celery_app, session, "orig", "alice")
        bulk_retry(celery_app, session, ["orig"], "alice")

    for task_id in ("started-1", "retry-1", "bulk-1"):
        assert _is_recent(_updated_at(Session, task_id))


def test_retry_bumps_a_row_the_listener_already_created(db_url):
    """retry_task's only write to an existing row is `retried_from_id` —
    that still has to reach the dashboard."""
    Session = _factory(db_url)
    celery_app = MagicMock()
    celery_app.send_task.return_value = MagicMock(id="child")
    _seed(Session, "parent", status="failed")
    _seed(Session, "child", status="queued")

    with Session() as session:
        retry_task(celery_app, session, "parent", "alice")
    assert _is_recent(_updated_at(Session, "child"))


# --- the change feed ----------------------------------------------------------


def test_feed_starts_at_the_head_and_reports_only_later_changes(db_url):
    Session = _factory(db_url)
    _seed(Session, "before")
    feed = RunChangeFeed()
    with Session() as session:
        assert feed.poll(session) == []  # priming poll: no replay of history

    writer = _factory(db_url)
    with writer() as session:
        upsert_task_run(session, {"type": "task-started", "uuid": "before"})
        upsert_task_run(session, {"type": "task-sent", "uuid": "after", "name": "demo.echo"})
        session.commit()

    with Session() as session:
        events = feed.poll(session)
        assert [(e["task_id"], e["status"]) for e in events] == [("before", "started"), ("after", "queued")]
        assert all(e["type"] == "task-updated" for e in events)
        assert feed.poll(session) == []  # nothing new, nothing repeated


def test_feed_pages_through_rows_sharing_one_timestamp(db_url):
    """A burst stamped with the same instant (whole-second DATETIME on
    MySQL, or just a fast writer) must be paged by (updated_at, id), not by
    updated_at alone — the latter skips the rest of the burst."""
    Session = _factory(db_url)
    _seed(Session, "head", updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    feed = RunChangeFeed(batch_size=2)
    with Session() as session:
        feed.poll(session)

    same = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    for n in range(5):
        _seed(Session, f"burst-{n}", updated_at=same)

    with Session() as session:
        events = feed.poll(session)
        assert sorted(e["task_id"] for e in events) == [f"burst-{n}" for n in range(5)]
        assert feed.poll(session) == []


def test_feed_catches_a_late_commit_behind_the_cursor_exactly_once(db_url):
    """`updated_at` is stamped at flush, not commit: a slow transaction can
    commit a timestamp older than one the feed has already passed."""
    Session = _factory(db_url)
    base = datetime.now(timezone.utc)
    feed = RunChangeFeed()
    with Session() as session:
        feed.poll(session)

    _seed(Session, "fast", updated_at=base)
    with Session() as session:
        assert [e["task_id"] for e in feed.poll(session)] == ["fast"]

    _seed(Session, "slow", updated_at=base - timedelta(seconds=1))
    with Session() as session:
        assert [e["task_id"] for e in feed.poll(session)] == ["slow"]
        assert feed.poll(session) == []


def test_feed_reports_a_second_write_to_the_same_row(db_url):
    Session = _factory(db_url)
    feed = RunChangeFeed()
    with Session() as session:
        feed.poll(session)
    for status in ("started", "succeeded"):
        with Session() as session:
            upsert_task_run(session, {"type": f"task-{status}", "uuid": "twice", "name": "demo.echo"})
            session.commit()
        with Session() as session:
            assert [(e["task_id"], e["status"]) for e in feed.poll(session)] == [("twice", status)]


# --- the poller on the EventBus ------------------------------------------------


def _poller_threads():
    return [t for t in threading.enumerate() if t.name == "celery-manager-change-feed" and t.is_alive()]


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_poller_publishes_another_processes_write_to_a_subscriber(db_url):
    bus = EventBus(_factory(db_url), poll_interval=0.05)
    other_process = _factory(db_url)

    async def run():
        queue = bus.subscribe()
        try:
            await asyncio.sleep(0.2)  # let the poller prime at the current head
            with other_process() as session:
                upsert_task_run(session, {"type": "task-started", "uuid": "remote", "name": "demo.echo"})
                session.commit()
            return await asyncio.wait_for(queue.get(), timeout=5)
        finally:
            bus.unsubscribe(queue)

    event = asyncio.run(run())
    assert (event["task_id"], event["status"], event["type"]) == ("remote", "started", "task-updated")


def test_poller_runs_only_while_someone_is_subscribed(db_url):
    bus = EventBus(_factory(db_url), poll_interval=0.05)
    assert not bus.polling and _poller_threads() == []

    async def run():
        first = bus.subscribe()
        second = bus.subscribe()
        assert bus.polling
        assert _wait_until(lambda: len(_poller_threads()) == 1)  # one per process, not per subscriber
        bus.unsubscribe(first)
        assert bus.polling
        bus.unsubscribe(second)
        assert not bus.polling

    asyncio.run(run())
    assert _wait_until(lambda: _poller_threads() == [])


def test_bus_without_a_session_factory_never_polls():
    bus = EventBus()

    async def run():
        queue = bus.subscribe()
        assert not bus.polling
        bus.unsubscribe(queue)

    asyncio.run(run())


def test_listener_no_longer_publishes_directly_so_events_are_not_doubled():
    from celery_manager.events.listener import EventListener

    bus = MagicMock()
    listener = EventListener(MagicMock(), MagicMock(), event_bus=bus)
    assert not hasattr(listener, "_event_bus")


def test_api_stream_delivers_a_write_from_another_process(db_url):
    """End to end through create_app(): GET /api/stream on the API process,
    a write on a separate engine, the event arrives on the stream."""
    from fastapi.testclient import TestClient

    from tests.unit.test_api_sse import _stream_scope

    config = CeleryManagerConfig(
        celery_app=Celery("test"), db_url=db_url, secret_key="test-secret", live_poll_interval_seconds=0.05
    )
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
    token = TestClient(app, base_url="https://testserver").post(
        "/api/auth/login", json={"username": "alice", "password": "pw"}
    ).cookies["cm_session"]
    other_process = _factory(db_url)

    async def run():
        messages: asyncio.Queue = asyncio.Queue()
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)

        async def send(message):
            await messages.put(message)

        scope = _stream_scope([(b"cookie", f"cm_session={token}".encode())])
        task = asyncio.create_task(app(scope, receive, send))
        try:
            assert (await asyncio.wait_for(messages.get(), timeout=5))["status"] == 200
            await asyncio.sleep(0.3)  # poller primed
            with other_process() as session:
                upsert_task_run(session, {"type": "task-succeeded", "uuid": "e2e", "name": "demo.echo"})
                session.commit()
            while True:
                body = (await asyncio.wait_for(messages.get(), timeout=5))["body"].decode()
                if body.startswith("data: "):
                    return json.loads(body[len("data: "):].strip())
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    event = asyncio.run(run())
    assert (event["task_id"], event["status"]) == ("e2e", "succeeded")
    assert not app.state.cm_event_bus.polling  # the disconnect stopped the poller


# --- migration ----------------------------------------------------------------


def test_migration_backfills_updated_at_from_the_latest_known_timestamp(db_url):
    engine = create_engine(db_url)
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "0002")
        conn.execute(text(
            "INSERT INTO task_runs (task_id, task_name, status, args, kwargs, retry_count, started_at, finished_at) VALUES "
            "('done', 'x', 'succeeded', '[]', '{}', 0, '2026-01-01 00:00:00.000000', '2026-01-01 00:00:05.000000'),"
            "('running', 'x', 'started', '[]', '{}', 0, '2026-01-02 00:00:00.000000', NULL),"
            "('queued', 'x', 'queued', '[]', '{}', 0, NULL, NULL)"
        ))
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "0003")

    Session = sessionmaker(bind=engine)
    assert _updated_at(Session, "done") == datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
    assert _updated_at(Session, "running") == datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert _is_recent(_updated_at(Session, "queued"))

    # And the column is now NOT NULL with a default, for rows inserted by hand.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO task_runs (task_id, task_name, status, args, kwargs, retry_count) "
            "VALUES ('raw', 'x', 'queued', '[]', '{}', 0)"
        ))
    assert _is_recent(_updated_at(Session, "raw"))
