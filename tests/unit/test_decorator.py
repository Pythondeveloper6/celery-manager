# tests/unit/test_decorator.py
from datetime import datetime, timedelta, timezone

import pytest
from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager import decorator as decorator_module
from celery_manager.db.models import Base, ScheduleOverride, TaskRun
from celery_manager.decorator import track, configure
from celery_manager.events.listener import upsert_task_run


class BrokenStrError(Exception):
    """An exception whose `__str__` raises — the shape that used to make the
    decorator's own failure branch blow up. Declared at module level rather
    than inside the test so it is an ordinary, importable class."""

    def __str__(self):
        raise RuntimeError("__str__ is broken")


@pytest.fixture
def env():
    app = Celery("test_app", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    configure(session_factory=Session, eta_tolerance_seconds=60)

    calls = []

    @app.task(name="demo.echo", bind=True)
    @track(schedule_name="demo-every-2h")
    def echo(self, value):
        calls.append(value)
        return value

    return app, Session, echo, calls


def test_runs_normally_when_no_override(env):
    app, Session, echo, calls = env
    echo.apply(args=["hi"])
    assert calls == ["hi"]


def test_skips_when_override_matches_now(env):
    app, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime.now(timezone.utc),
            action="skip",
            consumed=False,
            created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    echo.apply(args=["hi"])
    assert calls == []  # task body did not run

    with Session() as session:
        run = session.query(TaskRun).filter_by(task_name="demo.echo").one()
        assert run.status == "skipped"

        override = session.query(ScheduleOverride).filter_by(schedule_name="demo-every-2h").one()
        assert override.consumed is True


def test_ignores_override_outside_tolerance_window(env):
    app, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime.now(timezone.utc) - timedelta(hours=1),
            action="skip",
            consumed=False,
            created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    echo.apply(args=["hi"])
    assert calls == ["hi"]  # override is stale, task ran


# --- C1 regressions: decorator/events-listener collision on task_runs.task_id ---
#
# `task_runs.task_id` is UNIQUE, and in the documented deployment topology the
# events listener runs as its own process next to the worker — so it can insert
# a row for a task's UUID (from task-sent/task-started) *before* the decorated
# body finishes and the decorator writes its own outcome. The decorator used to
# blindly session.add(TaskRun(...)), which raised IntegrityError and made a
# task whose body SUCCEEDED report as FAILURE. Reproduced directly below.


def test_listener_row_then_decorated_run_converges_to_one_row(env):
    """(a) A listener-inserted row followed by that same task_id's decorated
    run must converge to ONE row carrying the decorator's final outcome —
    not an IntegrityError, and not a second row."""
    app, Session, echo, calls = env
    task_id = "shared-uuid-1"

    # The events listener process gets there first.
    with Session() as session:
        upsert_task_run(session, {"type": "task-sent", "uuid": task_id, "name": "demo.echo", "queue": "default"})
        upsert_task_run(session, {"type": "task-started", "uuid": task_id, "hostname": "worker-1"})
        session.commit()

    result = echo.apply(args=["hi"], task_id=task_id)

    # The task itself must be unaffected by who else wrote to storage.
    assert result.state == "SUCCESS"
    assert result.result == "hi"
    assert calls == ["hi"]

    with Session() as session:
        rows = session.query(TaskRun).filter_by(task_id=task_id).all()
        assert len(rows) == 1
        row = rows[0]
        # Decorator owns the final status transition and log capture...
        assert row.status == "succeeded"
        assert row.log_output is not None
        # ...but merges rather than overwrites: the listener owns worker/queue,
        # and a decorator write must never null those back out.
        assert row.worker == "worker-1"
        assert row.queue == "default"


def test_listener_row_then_failing_decorated_run_records_failure_on_same_row(env):
    """The failure branch upserts too — and the task's real exception still
    propagates rather than being displaced by a storage error."""
    app, Session, echo, calls = env
    task_id = "shared-uuid-2"

    @app.task(name="demo.boom", bind=True)
    @track()
    def boom(self):
        raise ValueError("kaboom")

    with Session() as session:
        upsert_task_run(session, {"type": "task-sent", "uuid": task_id, "name": "demo.boom", "queue": "default"})
        session.commit()

    result = boom.apply(task_id=task_id)

    assert result.state == "FAILURE"
    assert isinstance(result.result, ValueError)
    assert str(result.result) == "kaboom"

    with Session() as session:
        rows = session.query(TaskRun).filter_by(task_id=task_id).all()
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert "kaboom" in rows[0].exception
        assert "ValueError" in rows[0].traceback


def test_skip_path_consumes_override_even_if_task_run_write_fails(env, monkeypatch):
    """(b) The skip decision must genuinely survive a downstream storage
    error. `consumed = True` is committed on its own, before the task_runs
    write — otherwise a failed write would roll it back and the same override
    would silently skip a *later*, unrelated run of this schedule."""
    app, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime.now(timezone.utc),
            action="skip",
            consumed=False,
            created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    def exploding_upsert(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(decorator_module, "_upsert_task_run", exploding_upsert)

    result = echo.apply(args=["hi"], task_id="skip-uuid-1")

    # Body still skipped, and the storage failure did not surface as a task failure.
    assert calls == []
    assert result.state == "SUCCESS"

    with Session() as session:
        override = session.query(ScheduleOverride).filter_by(schedule_name="demo-every-2h").one()
        assert override.consumed is True


def test_persistence_failure_never_turns_a_successful_task_into_a_failure(env, monkeypatch):
    """Spec §4.1: celery-manager must never be the reason a task's reported
    outcome changes."""
    app, Session, echo, calls = env

    def exploding_upsert(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(decorator_module, "_upsert_task_run", exploding_upsert)

    result = echo.apply(args=["hi"], task_id="ok-uuid-1")

    assert calls == ["hi"]
    assert result.state == "SUCCESS"
    assert result.result == "hi"


def test_persistence_failure_still_propagates_a_real_task_exception(env, monkeypatch):
    """The mirror of the above: swallowing storage errors must not swallow
    the task's own exception."""
    app, Session, echo, calls = env

    @app.task(name="demo.boom2", bind=True)
    @track()
    def boom(self):
        raise ValueError("real failure")

    def exploding_upsert(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(decorator_module, "_upsert_task_run", exploding_upsert)

    result = boom.apply(task_id="boom-uuid-1")

    assert result.state == "FAILURE"
    assert isinstance(result.result, ValueError)
    assert str(result.result) == "real failure"


def test_unconfigured_decorator_still_runs_the_task_body(env, monkeypatch):
    """The common single-process/eager-testing case (no listener, never
    configured) must keep working exactly as before: pass-through, no raise."""
    app, Session, echo, calls = env
    monkeypatch.setattr(decorator_module, "_session_factory", None)

    result = echo.apply(args=["hi"])

    assert calls == ["hi"]
    assert result.state == "SUCCESS"
    with Session() as session:
        assert session.query(TaskRun).count() == 0


# --- the up-front `started` row ---------------------------------------------
#
# The decorator used to write to task_runs only when a task *finished*, which
# made every long-running task invisible in the dashboard unless the separate
# events-listener process happened to be running — and left
# `report_progress()` with no row to write to until the run was already over.
# It now upserts a `started` row before invoking the body. Cost: one extra
# commit per tracked run.


def test_started_row_exists_while_the_task_is_mid_flight(env):
    """The assertion that actually proves the feature: the row is readable
    from another session *during* the body, not merely afterwards."""
    app, Session, echo, calls = env
    seen = {}

    @app.task(name="demo.slow", bind=True)
    @track(schedule_name="demo-slow")
    def slow(self, value):
        with Session() as session:
            row = session.query(TaskRun).filter_by(task_id="mid-flight-1").one()
            seen["status"] = row.status
            seen["task_name"] = row.task_name
            seen["schedule_name"] = row.schedule_name
            seen["args"] = row.args
            seen["started_at_set"] = row.started_at is not None
            seen["finished_at"] = row.finished_at
        return value

    result = slow.apply(args=["hi"], task_id="mid-flight-1")

    assert result.state == "SUCCESS"
    assert seen == {
        "status": "started",
        "task_name": "demo.slow",
        "schedule_name": "demo-slow",
        "args": '["hi"]',
        "started_at_set": True,
        "finished_at": None,
    }


def test_terminal_status_still_wins_over_the_started_row(env):
    """started -> succeeded / failed, on one row, in that order. The two
    writes happen in the same thread around the body, so the terminal one
    always lands last."""
    app, Session, echo, calls = env

    echo.apply(args=["hi"], task_id="terminal-1")

    @app.task(name="demo.boom3", bind=True)
    @track()
    def boom(self):
        raise ValueError("kaboom")

    boom.apply(task_id="terminal-2")

    with Session() as session:
        ok = session.query(TaskRun).filter_by(task_id="terminal-1").one()
        assert ok.status == "succeeded"
        assert ok.finished_at is not None

        bad = session.query(TaskRun).filter_by(task_id="terminal-2").one()
        assert bad.status == "failed"
        assert "kaboom" in bad.exception


def test_started_row_upserts_onto_a_listener_row_without_clobbering_it(env):
    """Same collision the C1 regressions above cover, now on the *first*
    write rather than the last: the listener may already have inserted this
    uuid from task-sent/task-started, so the started write must merge (one
    row, listener-owned worker/queue intact), not insert."""
    app, Session, echo, calls = env
    task_id = "started-vs-listener-1"

    with Session() as session:
        upsert_task_run(session, {"type": "task-sent", "uuid": task_id, "name": "demo.echo", "queue": "default"})
        upsert_task_run(session, {"type": "task-started", "uuid": task_id, "hostname": "worker-1"})
        session.commit()

    result = echo.apply(args=["hi"], task_id=task_id)

    assert result.state == "SUCCESS"
    with Session() as session:
        rows = session.query(TaskRun).filter_by(task_id=task_id).all()
        assert len(rows) == 1
        assert rows[0].status == "succeeded"
        assert rows[0].worker == "worker-1"
        assert rows[0].queue == "default"


def test_a_skipped_run_never_reports_itself_as_started(env):
    """The started write sits after the skip-check, so a paused occurrence
    goes straight to `skipped` and never flickers through `started` — which
    matters because the listener guards `skipped` and nothing else."""
    app, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime.now(timezone.utc),
            action="skip",
            consumed=False,
            created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    echo.apply(args=["hi"], task_id="skipped-not-started-1")

    assert calls == []
    with Session() as session:
        row = session.query(TaskRun).filter_by(task_id="skipped-not-started-1").one()
        assert row.status == "skipped"


def test_a_failed_started_write_does_not_stop_the_run_or_the_final_write(env, monkeypatch):
    """Fail-open, on the new write too: if the up-front row cannot be stored,
    the body still runs and the outcome is still recorded."""
    app, Session, echo, calls = env
    real_upsert = decorator_module._upsert_task_run
    attempts = []

    def explode_on_first_write(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:  # the `started` write
            raise RuntimeError("storage is down")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(decorator_module, "_upsert_task_run", explode_on_first_write)

    result = echo.apply(args=["hi"], task_id="started-write-failed-1")

    assert calls == ["hi"]
    assert result.state == "SUCCESS"
    with Session() as session:
        row = session.query(TaskRun).filter_by(task_id="started-write-failed-1").one()
        assert row.status == "succeeded"


# --- arguments the decorator has to store but cannot serialize --------------
#
# `@track` records the run's args/kwargs, and it does that *before* calling
# the body. Stdlib `json.dumps` is stricter than Celery: kombu's JSON
# serializer round-trips datetime/date/UUID/Decimal over the broker, so a
# task can legitimately arrive in the worker holding objects stdlib json
# refuses. That used to raise straight out of the wrapper — the body never
# ran and the task reported FAILURE, which is exactly the outcome spec §4.1
# says celery-manager must never cause.


def test_unserializable_args_do_not_stop_the_task_body(env):
    from decimal import Decimal
    from uuid import UUID

    app, Session, echo, calls = env
    ran = []

    @app.task(name="demo.exotic_args", bind=True)
    @track()
    def exotic(self, when, ident, amount):
        ran.append(when)
        return "ok"

    result = exotic.apply(
        args=[datetime(2026, 1, 1, tzinfo=timezone.utc),
              UUID("00000000-0000-0000-0000-000000000001"),
              Decimal("1.50")],
        task_id="exotic-1",
    )

    assert result.state == "SUCCESS"
    assert result.result == "ok"
    assert len(ran) == 1
    with Session() as session:
        row = session.query(TaskRun).filter_by(task_id="exotic-1").one()
        assert row.status == "succeeded"
        # Shape preserved (a list stays a list), unserializable leaves
        # replaced by their repr so the value is still searchable.
        assert row.args.startswith("[") and row.args.endswith("]")
        # repr, not JSON, for the leaves stdlib json cannot express.
        assert "datetime.datetime(2026, 1, 1" in row.args
        assert "1.50" in row.args


def test_a_hostile_object_in_kwargs_still_does_not_stop_the_task(env):
    app, Session, echo, calls = env

    class Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    @app.task(name="demo.hostile_args", bind=True)
    @track()
    def hostile(self, **kwargs):
        return "ok"

    result = hostile.apply(kwargs={"thing": Hostile()}, task_id="hostile-1")

    assert result.state == "SUCCESS"
    with Session() as session:
        assert session.query(TaskRun).filter_by(task_id="hostile-1").one().status == "succeeded"


# --- retries: Celery reuses the task id across attempts ---------------------


def test_a_celery_retry_is_recorded_as_retried_not_failed(env):
    """`self.retry()` raises `celery.exceptions.Retry`, an ordinary Exception
    subclass. Caught by the generic failure branch it recorded a run Celery
    considers RETRY as `failed`, with "Retry in 30s: ..." sitting in the
    exception column and a finish time for a run that had not finished."""
    app, Session, echo, calls = env
    writes = []

    @app.task(name="demo.retrier", bind=True, max_retries=2, default_retry_delay=0)
    @track()
    def retrier(self):
        if self.request.retries < 1:
            raise self.retry(exc=ValueError("transient"))
        return "ok"

    real_persist = decorator_module._persist_run

    def spy(task_id, **fields):
        writes.append(fields.get("status"))
        return real_persist(task_id, **fields)

    original = decorator_module._persist_run
    decorator_module._persist_run = spy
    try:
        result = retrier.apply(task_id="retry-status-1")
    finally:
        decorator_module._persist_run = original

    assert result.state == "SUCCESS"
    # Never `failed` — the intermediate attempt is a retry, and the run ends
    # succeeded.
    assert writes == ["started", "retried", "started", "succeeded"]


def test_a_new_attempt_clears_the_previous_attempts_outcome(env):
    """Celery reuses the task id, so attempt N+1 writes onto attempt N's row.
    `_upsert_task_run` skips None values and therefore cannot erase a field —
    without the explicit `clear`, a run that failed once and then succeeded
    kept the failure's exception, traceback and progress counters and
    rendered as `succeeded` with a traceback attached."""
    app, Session, echo, calls = env
    attempts = []

    @app.task(name="demo.autoretry", bind=True,
              autoretry_for=(ValueError,), max_retries=2, default_retry_delay=0)
    @track()
    def flaky(self):
        attempts.append(1)
        if len(attempts) < 2:
            raise ValueError("transient")
        return "ok"

    result = flaky.apply(task_id="clear-1")

    assert result.state == "SUCCESS"
    with Session() as session:
        row = session.query(TaskRun).filter_by(task_id="clear-1").one()
        assert row.status == "succeeded"
        assert row.exception is None
        assert row.traceback is None
        # The successful attempt's own timings survive; only the stale ones go.
        assert row.finished_at is not None
        assert row.runtime_seconds is not None


def test_clearing_on_a_new_attempt_never_touches_listener_owned_fields(env):
    """The `clear` list is deliberately limited to what the decorator itself
    writes. worker/queue belong to the events listener and must survive every
    attempt."""
    app, Session, echo, calls = env
    task_id = "clear-vs-listener-1"

    with Session() as session:
        upsert_task_run(session, {"type": "task-sent", "uuid": task_id, "name": "demo.echo", "queue": "default"})
        upsert_task_run(session, {"type": "task-started", "uuid": task_id, "hostname": "worker-1"})
        session.commit()

    echo.apply(args=["hi"], task_id=task_id)

    with Session() as session:
        row = session.query(TaskRun).filter_by(task_id=task_id).one()
        assert row.worker == "worker-1"
        assert row.queue == "default"
        assert row.status == "succeeded"


# --- @track on a task that is not `bind=True` -------------------------------
#
# The decorator is documented to sit under `@app.task(bind=True)` and used to
# assume args[0] was the Task instance, reading `args[0].request.id`. Applied
# to an unbound task that assumption raised `AttributeError: 'str' object has
# no attribute 'request'` out of the wrapper — a wiring mistake failing the
# user's task before its body ever ran.


def test_track_on_an_unbound_task_still_runs_the_body(env):
    app, Session, echo, calls = env
    ran = []

    @app.task(name="demo.unbound")  # deliberately not bind=True
    @track()
    def unbound(value):
        ran.append(value)
        return value

    result = unbound.apply(args=["hi"], task_id="unbound-1")

    assert result.state == "SUCCESS"
    assert result.result == "hi"
    assert ran == ["hi"]


def test_track_on_an_unbound_task_records_the_real_name_and_uuid(env):
    """Falling back to `celery.current_task` (the same guarded lookup
    `progress._resolve_run` uses) means an unbound task is recorded properly,
    not merely without crashing: real registered name, real uuid, and all of
    its arguments — none of which is args[0]-is-self."""
    app, Session, echo, calls = env

    @app.task(name="demo.unbound_named")
    @track()
    def unbound(a, b):
        return a + b

    unbound.apply(args=[1, 2], task_id="unbound-2")

    with Session() as session:
        row = session.query(TaskRun).filter_by(task_id="unbound-2").one()
        assert row.task_name == "demo.unbound_named"
        assert row.status == "succeeded"
        assert row.args == "[1, 2]"


def test_track_on_an_unbound_task_with_a_first_arg_that_looks_bound(env):
    """The old positional assumption was type-dependent: an argument that
    happened to expose `.request` would have been silently mistaken for the
    task instance and dropped from the recorded args."""
    app, Session, echo, calls = env

    class LooksLikeATask:
        request = object()
        name = "not.a.task"

        def __repr__(self):
            return "<LooksLikeATask>"

    @app.task(name="demo.decoy")
    @track()
    def decoy(thing):
        return "ok"

    result = decoy.apply(args=[LooksLikeATask()], task_id="decoy-1")

    assert result.state == "SUCCESS"
    with Session() as session:
        row = session.query(TaskRun).filter_by(task_id="decoy-1").one()
        assert row.task_name == "demo.decoy"
        # The decoy is an argument, not the task, so it stays in args.
        assert "LooksLikeATask" in row.args


def test_an_exception_whose_str_raises_does_not_displace_the_real_failure(env):
    """The failure branch called `str(exc)` unguarded, so an exception class
    with a broken `__str__` (a real pattern in DB drivers and in types that
    skip `super().__init__`) raised *from inside the except block* and
    replaced the task's own exception — the caller saw a RuntimeError from
    celery-manager instead of their failure, and the row stayed at
    `started`."""
    app, Session, echo, calls = env

    @app.task(name="demo.bad_str", bind=True)
    @track()
    def bad(self):
        raise BrokenStrError("real failure")

    # `throw=True` so the task's own exception object reaches us intact:
    # a plain eager `.apply()` round-trips the result through Celery's
    # serialization, which would answer the question "can Celery pickle this
    # class" rather than the one this test is asking.
    with pytest.raises(BrokenStrError):
        bad.apply(task_id="bad-str-1", throw=True)

    with Session() as session:
        row = session.query(TaskRun).filter_by(task_id="bad-str-1").one()
        assert row.status == "failed"
        assert "BrokenStrError" in row.exception
        assert row.traceback is not None
