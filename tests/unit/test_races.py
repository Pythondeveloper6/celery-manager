"""Concurrent-writer races on `task_runs` (UNIQUE task_id).

Four writers create rows for the same uuid with no coordination: the events
listener, the `@track` decorator, `report_progress()`, and `retry_task`. The
old code in each was check-then-insert, so a competitor committing between
the check and the INSERT raised IntegrityError — a 500 (and lost lineage)
from the retry button, a dropped event in the listener.

Most tests here reproduce that window deterministically rather than hoping a
thread schedule hits it: `_race_on_first_task_runs_insert` hooks the engine
so that, the instant our writer is about to send its INSERT, a competitor on
a *separate* connection inserts and commits the same uuid. That is exactly
the interleaving a real worker produced. File-backed SQLite in tmp_path, so
the two connections are genuinely separate (an in-memory StaticPool DB would
share one connection and hide the race).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from celery import Celery
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.orm import sessionmaker

from celery_manager.control.tasks import (
    _decode_stored_call_argument,
    bulk_retry,
    get_task_run,
    retry_task,
)
from celery_manager.db import runs as runs_module
from celery_manager.db.models import AuditLog, Base, TaskRun
from celery_manager.db.runs import lock_task_run
from celery_manager.decorator import _upsert_task_run
from celery_manager.events.listener import _normalise_call_argument, upsert_task_run


@pytest.fixture
def db(tmp_path):
    """(our_session_factory, competitor_session_factory, our_engine) over one
    file-backed SQLite database, via two independent engines."""
    url = f"sqlite:///{tmp_path / 'races.db'}"
    ours = create_engine(url, connect_args={"check_same_thread": False})
    theirs = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(ours)
    yield (
        sessionmaker(bind=ours, expire_on_commit=False),
        sessionmaker(bind=theirs, expire_on_commit=False),
        ours,
    )
    ours.dispose()
    theirs.dispose()


def _race_on_first_task_runs_insert(engine, competitor):
    """Run `competitor()` (on another connection, committing) immediately
    before the first INSERT INTO task_runs on `engine` reaches the driver.

    `before_cursor_execute` fires before pysqlite emits its implicit BEGIN,
    so our connection holds no lock yet and the competitor can commit —
    i.e. the row lands after any check we made and before our insert.
    """
    fired = []

    def hook(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("INSERT INTO TASK_RUNS"):
            fired.append(True)
            competitor()

    event.listen(engine, "before_cursor_execute", hook)
    return fired


def _insert_with(session_factory, **fields):
    def competitor():
        with session_factory() as session:
            session.add(TaskRun(**fields))
            session.commit()

    return competitor


def _seed_failed_run(session_factory, task_id="orig-1"):
    with session_factory() as session:
        session.add(TaskRun(task_id=task_id, task_name="demo.echo", status="failed",
                            args='["x"]', kwargs="{}"))
        session.commit()


def _app_returning(*ids):
    app = MagicMock(spec=Celery)
    app.send_task.side_effect = [MagicMock(id=i) for i in ids]
    return app


# --- Bug 1: retry vs. the worker's own row ---


def test_retry_succeeds_when_the_workers_row_lands_mid_retry(db):
    ours, theirs, engine = db
    _seed_failed_run(ours)
    # The decorator's `started` row (worker + queue known) commits in the
    # window between dispatch and our lineage write.
    fired = _race_on_first_task_runs_insert(engine, _insert_with(
        theirs, task_id="orig-1-retry", task_name="demo.echo", status="started",
        args='["x"]', kwargs="{}", worker="worker-1@host", queue="default",
    ))

    with ours() as session:
        new_id = retry_task(_app_returning("orig-1-retry"), session, "orig-1", username="alice")

    assert fired, "the race window was never exercised"
    with ours() as session:
        rows = session.query(TaskRun).filter_by(task_id=new_id).all()
        assert len(rows) == 1
        run = rows[0]
        assert run.retried_from_id == "orig-1"  # lineage recorded regardless
        assert run.status == "started"  # our `queued` seed did not restate over it
        assert run.worker == "worker-1@host"
        assert run.queue == "default"
        assert [a.action for a in session.query(AuditLog).all()] == ["retry"]


def test_retry_never_downgrades_a_run_that_already_finished(db):
    """A fast task can be done before send_task() even returns."""
    ours, theirs, engine = db
    _seed_failed_run(ours)
    _race_on_first_task_runs_insert(engine, _insert_with(
        theirs, task_id="orig-1-retry", task_name="demo.echo", status="succeeded",
    ))

    with ours() as session:
        retry_task(_app_returning("orig-1-retry"), session, "orig-1", username="alice")
        run = get_task_run(session, "orig-1-retry")
        assert run.status == "succeeded"
        assert run.retried_from_id == "orig-1"


def test_bulk_retry_survives_a_race_on_one_item_and_completes_the_rest(db):
    """The old IntegrityError also poisoned the shared session, so every
    later item in the bulk request failed with PendingRollbackError."""
    ours, theirs, engine = db
    _seed_failed_run(ours, "a")
    _seed_failed_run(ours, "b")
    _race_on_first_task_runs_insert(engine, _insert_with(
        theirs, task_id="a-retry", task_name="demo.echo", status="queued", queue="default",
    ))

    with ours() as session:
        new_ids = bulk_retry(_app_returning("a-retry", "b-retry"), session, ["a", "b"], username="alice")

    assert new_ids == ["a-retry", "b-retry"]
    with ours() as session:
        assert get_task_run(session, "a-retry").retried_from_id == "a"
        assert get_task_run(session, "a-retry").queue == "default"
        assert get_task_run(session, "b-retry").retried_from_id == "b"
        assert session.query(AuditLog).count() == 2


# --- Bug 2: listener / decorator / progress get-or-create ---


def test_listener_task_sent_merges_into_a_row_the_decorator_inserted_mid_event(db):
    ours, theirs, engine = db
    fired = _race_on_first_task_runs_insert(engine, _insert_with(
        theirs, task_id="t-1", task_name="demo.echo", status="started",
        args='["x"]', kwargs='{"n": 1}', worker="worker-1@host",
    ))

    with ours() as session:
        upsert_task_run(session, {
            "type": "task-sent", "uuid": "t-1", "name": "demo.echo", "queue": "reports",
            # task-sent's hostname is the *producer's*, not a worker's.
            "hostname": "api-host", "args": "('x',)", "kwargs": "{'n': 1}",
        })
        session.commit()

    assert fired
    with ours() as session:
        run = get_task_run(session, "t-1")
        assert run.status == "started"  # stale task-sent did not downgrade
        assert run.queue == "reports"  # ...but its queue was merged in
        assert run.worker == "worker-1@host"
        assert run.args == '["x"]'
        assert run.kwargs == '{"n": 1}'


def test_decorator_started_write_merges_into_a_row_the_listener_inserted_mid_write(db):
    ours, theirs, engine = db
    fired = _race_on_first_task_runs_insert(engine, _insert_with(
        theirs, task_id="t-2", task_name="demo.echo", status="queued",
        queue="reports", worker="worker-1@host",
    ))

    with ours() as session:
        _upsert_task_run(session, "t-2", task_name="demo.echo", status="started",
                         args='["x"]', kwargs="{}", worker=None,
                         clear=("exception", "traceback"))
        session.commit()

    assert fired
    with ours() as session:
        run = get_task_run(session, "t-2")
        assert run.status == "started"
        assert run.queue == "reports"
        assert run.worker == "worker-1@host"  # None never nulls a known value
        assert run.args == '["x"]'


def test_report_progress_on_an_undecorated_task_races_the_listener(db, monkeypatch):
    from celery_manager import decorator, progress

    ours, theirs, engine = db
    monkeypatch.setattr(decorator, "_session_factory", ours)
    monkeypatch.setattr(progress, "_resolve_run", lambda: ("t-3", "demo.plain"))
    fired = _race_on_first_task_runs_insert(engine, _insert_with(
        theirs, task_id="t-3", task_name="demo.plain", status="succeeded", worker="w1",
    ))

    progress.report_progress(5, 10, "half")

    assert fired
    with ours() as session:
        run = get_task_run(session, "t-3")
        assert (run.progress_current, run.progress_total, run.progress_message) == (5, 10, "half")
        assert run.status == "succeeded"  # create-only seed never restated
        assert run.worker == "w1"


def test_stale_lifecycle_events_never_downgrade_a_finished_run(db):
    ours, _theirs, _engine = db
    finished = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    with ours() as session:
        session.add(TaskRun(task_id="t-4", task_name="demo.echo", status="succeeded",
                            started_at=finished - timedelta(seconds=3), finished_at=finished))
        session.commit()

    with ours() as session:
        for event_type in ("task-sent", "task-received", "task-started"):
            upsert_task_run(session, {"type": event_type, "uuid": "t-4", "hostname": "w2"})
        session.commit()

    with ours() as session:
        run = get_task_run(session, "t-4")
        assert run.status == "succeeded"
        # The stale task-started did not push started_at past finished_at.
        assert run.started_at.replace(tzinfo=timezone.utc) == finished - timedelta(seconds=3)
        assert run.worker == "w2"  # non-status facts still merge


def test_queued_never_overwrites_started_but_a_new_attempt_overwrites_retried(db):
    ours, _theirs, _engine = db
    with ours() as session:
        upsert_task_run(session, {"type": "task-started", "uuid": "t-5", "name": "demo.echo"})
        upsert_task_run(session, {"type": "task-sent", "uuid": "t-5"})
        session.commit()
        assert get_task_run(session, "t-5").status == "started"

        upsert_task_run(session, {"type": "task-retried", "uuid": "t-5"})
        upsert_task_run(session, {"type": "task-received", "uuid": "t-5"})
        session.commit()
        assert get_task_run(session, "t-5").status == "retried"

        upsert_task_run(session, {"type": "task-started", "uuid": "t-5"})
        upsert_task_run(session, {"type": "task-succeeded", "uuid": "t-5"})
        session.commit()
        assert get_task_run(session, "t-5").status == "succeeded"


def test_skipped_still_wins_over_every_event(db):
    ours, _theirs, _engine = db
    with ours() as session:
        session.add(TaskRun(task_id="t-6", task_name="demo.report", status="skipped"))
        session.commit()
        for event_type in ("task-sent", "task-started", "task-succeeded", "task-retried"):
            upsert_task_run(session, {"type": event_type, "uuid": "t-6"})
        session.commit()
        assert get_task_run(session, "t-6").status == "skipped"


def test_hammering_get_or_create_from_several_threads_never_collides(db):
    """Not deterministic like the tests above, but it is the real thing:
    every writer, concurrently, on the same uuids, separate connections."""
    ours, theirs, _engine = db
    task_ids = [f"h-{i}" for i in range(25)]
    _seed_failed_run(ours, "parent")
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def listener():
        for task_id in task_ids:
            with theirs() as session:
                upsert_task_run(session, {"type": "task-sent", "uuid": task_id, "name": "demo.echo",
                                          "queue": "q", "args": "('x',)"})
                session.commit()

    def decorator():
        for task_id in task_ids:
            with ours() as session:
                _upsert_task_run(session, task_id, task_name="demo.echo", status="started",
                                 args='["x"]', kwargs="{}")
                session.commit()

    def worker_events():
        for task_id in task_ids:
            with theirs() as session:
                upsert_task_run(session, {"type": "task-started", "uuid": task_id, "hostname": "w1"})
                session.commit()

    def retrier():
        app = _app_returning(*task_ids)
        for _ in task_ids:
            with ours() as session:
                retry_task(app, session, "parent", username="alice")

    def run(fn):
        try:
            barrier.wait()
            fn()
        except BaseException as exc:  # noqa: BLE001 — collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(fn,)) for fn in (listener, decorator, worker_events, retrier)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with ours() as session:
        for task_id in task_ids:
            rows = session.query(TaskRun).filter_by(task_id=task_id).all()
            assert len(rows) == 1, task_id
            assert rows[0].retried_from_id == "parent"
            assert rows[0].queue == "q"
            assert rows[0].worker == "w1"
            assert rows[0].status == "started"
            assert rows[0].args == '["x"]'


# --- lock_task_run itself: the non-SQLite paths ---


def test_generic_fallback_survives_a_lost_race_without_losing_pending_work(db, monkeypatch):
    """Dialects without a native upsert go through SAVEPOINT + IntegrityError.
    The savepoint must confine the failure so the caller's transaction (and
    whatever else it has pending) survives."""
    ours, theirs, _engine = db
    monkeypatch.setattr(runs_module, "_insert_if_absent", lambda dialect_name, values: None)
    # Committed before our transaction starts rather than via the mid-write
    # hook: our pending AuditLog is flushed (taking SQLite's write lock) as
    # the savepoint opens, so a competitor could not commit inside the
    # window anyway. The INSERT still loses on the UNIQUE key either way.
    _insert_with(theirs, task_id="g-1", task_name="demo.echo", status="started", worker="w1")()

    with ours() as session:
        session.add(AuditLog(username="alice", action="x", target_type="task", target_id="g-1",
                             created_at=datetime.now(timezone.utc)))
        run = lock_task_run(session, "g-1", seed={"task_name": "demo.echo", "status": "queued"})
        run.retried_from_id = "parent"
        session.commit()

    with ours() as session:
        run = get_task_run(session, "g-1")
        assert (run.status, run.worker, run.retried_from_id) == ("started", "w1", "parent")
        assert session.query(AuditLog).count() == 1


def test_generic_fallback_still_raises_a_real_integrity_error(db, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    ours, _theirs, _engine = db
    monkeypatch.setattr(runs_module, "_insert_if_absent", lambda dialect_name, values: None)
    with ours() as session, pytest.raises(IntegrityError):
        lock_task_run(session, "g-2", seed={"task_name": "demo.echo"})  # status is NOT NULL


@pytest.mark.parametrize(("dialect", "expected"), [
    (postgresql.dialect(), "ON CONFLICT (task_id) DO NOTHING"),
    (mysql.dialect(), "ON DUPLICATE KEY UPDATE task_id = VALUES(task_id)"),
])
def test_native_insert_if_absent_compiles_for_server_databases(dialect, expected):
    stmt = runs_module._insert_if_absent(dialect.name, {"task_id": "x", "task_name": "n", "status": "queued"})
    assert expected in str(stmt.compile(dialect=dialect))


def test_unknown_dialect_uses_the_generic_fallback():
    assert runs_module._insert_if_absent("mssql", {"task_id": "x"}) is None


# --- Bug 3: args/kwargs normalisation ---


@pytest.mark.parametrize(("raw", "expected"), [
    ("('x',)", '["x"]'),
    ("(5, 'x')", '[5, "x"]'),
    ("{'b': 2}", '{"b": 2}'),
    ("()", "[]"),
    ("[1, None, True]", "[1, null, true]"),
    # Not convertible: kept verbatim rather than guessed at.
    ("('aaaa', ...", "('aaaa', ..."),  # truncated by Celery's argsrepr_maxsize
    ("({1, 2},)", "({1, 2},)"),  # a set has no JSON form
    ("(<Row object at 0x7f>,)", "(<Row object at 0x7f>,)"),
    # Already structured (not a repr string).
    (("x", 1), '["x", 1]'),
    ({"a": 1}, '{"a": 1}'),
])
def test_normalise_call_argument(raw, expected):
    assert _normalise_call_argument(raw) == expected


def test_listener_stores_repr_args_as_json_and_retry_can_still_decode_them(db):
    ours, _theirs, _engine = db
    with ours() as session:
        upsert_task_run(session, {"type": "task-received", "uuid": "a-1", "name": "demo.plain",
                                  "args": "(5, 'x')", "kwargs": "{'b': 2}"})
        session.commit()
        run = get_task_run(session, "a-1")
        assert run.args == '[5, "x"]'
        assert run.kwargs == '{"b": 2}'
        assert _decode_stored_call_argument(run.args, "args", "a-1", []) == [5, "x"]


def test_listener_never_replaces_decorator_json_args_with_its_repr(db):
    ours, _theirs, _engine = db
    with ours() as session:
        # The decorator saw the full value; Celery's repr of it is truncated.
        session.add(TaskRun(task_id="a-2", task_name="demo.echo", status="started",
                            args='["' + "a" * 2000 + '"]', kwargs='{"k": 1}'))
        session.commit()
        upsert_task_run(session, {"type": "task-received", "uuid": "a-2",
                                  "args": "('aaaa...", "kwargs": "{'k': 1}"})
        session.commit()
        run = get_task_run(session, "a-2")
        assert run.args == '["' + "a" * 2000 + '"]'
        assert run.kwargs == '{"k": 1}'


def test_listener_upgrades_empty_default_and_legacy_repr_args(db):
    ours, _theirs, _engine = db
    with ours() as session:
        session.add_all([
            # Seeded with the column defaults (e.g. by report_progress()).
            TaskRun(task_id="a-3", task_name="demo.echo", status="started"),
            # Written as a repr by an older listener.
            TaskRun(task_id="a-4", task_name="demo.echo", status="queued", args="('x',)", kwargs="{}"),
        ])
        session.commit()
        upsert_task_run(session, {"type": "task-started", "uuid": "a-3", "args": "(1,)", "kwargs": "{'z': 0}"})
        upsert_task_run(session, {"type": "task-started", "uuid": "a-4", "args": "('x',)"})
        session.commit()
        assert (get_task_run(session, "a-3").args, get_task_run(session, "a-3").kwargs) == ("[1]", '{"z": 0}')
        assert get_task_run(session, "a-4").args == '["x"]'
