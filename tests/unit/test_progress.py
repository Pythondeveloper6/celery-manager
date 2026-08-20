# tests/unit/test_progress.py
#
# `report_progress()` is called from inside somebody else's task body, in a
# loop, possibly with arguments they got wrong. Two properties matter more
# than any individual behaviour here and most of the file is about them:
#
#   * it never raises into the caller's task (spec §4.1 — celery-manager must
#     never be the reason a task's outcome changes), and
#   * it does not turn a 100k-iteration loop into 100k commits.
#
# The fixture points the decorator's module-global session factory at an
# in-memory database with `monkeypatch` rather than calling `configure()`, so
# that a failing test cannot leak a stale factory (or a stale
# `_warned_*` flag) into whatever runs next.

import time

import pytest
from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager import decorator as decorator_module
from celery_manager import progress as progress_module
from celery_manager import report_progress
from celery_manager.db.models import Base, TaskRun
from celery_manager.decorator import track
from celery_manager.events.listener import upsert_task_run


@pytest.fixture
def env(monkeypatch):
    app = Celery("progress_test_app", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setattr(decorator_module, "_session_factory", Session)
    monkeypatch.setattr(decorator_module, "_eta_tolerance_seconds", 60)
    # The warn-once flags are process-global by design (one WARNING per
    # worker, not one per call). Reset them per test so ordering can't decide
    # whether a test sees its warning.
    monkeypatch.setattr(progress_module, "_warned_no_task_id", False)
    monkeypatch.setattr(progress_module, "_warned_unconfigured", False)

    return app, Session


def _row(Session, task_id):
    with Session() as session:
        return session.query(TaskRun).filter_by(task_id=task_id).one()


# --- the happy path, inside a real eager-mode task ---------------------------


def test_progress_lands_on_the_task_run_row(env):
    app, Session = env

    @app.task(name="progress.import", bind=True)
    @track()
    def import_rows(self, n):
        report_progress(1, n, "row 1")
        return "done"

    result = import_rows.apply(args=[10], task_id="p-1")

    assert result.state == "SUCCESS"
    row = _row(Session, "p-1")
    assert (row.progress_current, row.progress_total) == (1, 10)
    assert row.progress_message == "row 1"
    assert row.progress_updated_at is not None


def test_progress_is_visible_while_the_task_is_still_running(env):
    """The whole point of the feature: the numbers must be readable from
    another connection *during* the run, not only once it has finished."""
    app, Session = env
    seen = []

    @app.task(name="progress.live", bind=True)
    @track()
    def live(self, n):
        for i in range(1, n + 1):
            report_progress(i, n, f"row {i}")
            row = _row(Session, "p-live")
            seen.append((row.status, row.progress_current, row.progress_total))
        return "done"

    live.apply(args=[1], task_id="p-live")

    assert seen == [("started", 1, 1)]


def test_progress_survives_the_terminal_write(env):
    """The decorator's succeeded write must not null out progress — it skips
    None values, so the fields it never mentions are left alone."""
    app, Session = env

    @app.task(name="progress.keep", bind=True)
    @track()
    def keep(self):
        report_progress(7, 7, "all done")
        return "ok"

    keep.apply(task_id="p-keep")

    row = _row(Session, "p-keep")
    assert row.status == "succeeded"
    assert (row.progress_current, row.progress_total, row.progress_message) == (7, 7, "all done")


def test_progress_from_a_task_not_wrapped_in_track(env):
    """Fallback path: no @track ContextVar, so the id comes from
    `celery.current_task.request.id`. Nothing else has created a row for that
    uuid here (no listener running), so report_progress has to insert one —
    and `task_runs.task_name`/`.status` are NOT NULL, so it must seed them."""
    app, Session = env

    @app.task(name="progress.untracked", bind=True)
    def untracked(self):
        report_progress(4, 8, "half way")
        return "ok"

    result = untracked.apply(task_id="p-untracked")

    assert result.state == "SUCCESS"
    row = _row(Session, "p-untracked")
    assert row.task_name == "progress.untracked"
    assert row.status == "started"
    assert (row.progress_current, row.progress_total) == (4, 8)


def test_seeded_fields_never_overwrite_an_existing_row(env):
    """`create_defaults` is create-only: progress landing on a row the events
    listener already owns must not restate task_name/status over it."""
    app, Session = env
    task_id = "p-listener-first"

    with Session() as session:
        upsert_task_run(session, {
            "type": "task-started",
            "uuid": task_id,
            "name": "progress.real_name",
            "hostname": "worker-1",
        })
        session.commit()

    @app.task(name="progress.other_name", bind=True)
    def untracked(self):
        report_progress(1, 2)
        return "ok"

    untracked.apply(task_id=task_id)

    row = _row(Session, task_id)
    assert row.task_name == "progress.real_name"
    assert row.worker == "worker-1"
    assert row.progress_current == 1


# --- throttling --------------------------------------------------------------


def test_throttle_suppresses_a_rapid_second_write(env, monkeypatch):
    app, Session = env
    # A window nothing in this test can outlive, so the assertion is about the
    # throttle and not about how fast the machine running CI happens to be.
    monkeypatch.setattr(progress_module, "MIN_WRITE_INTERVAL_SECONDS", 3600)

    @app.task(name="progress.tight_loop", bind=True)
    @track()
    def tight_loop(self):
        report_progress(1, 100, "first")
        report_progress(2, 100, "second")
        report_progress(3, 100, "third")
        return "ok"

    tight_loop.apply(task_id="p-throttle")

    row = _row(Session, "p-throttle")
    assert (row.progress_current, row.progress_message) == (1, "first")


def test_throttle_never_suppresses_the_completion_write(env, monkeypatch):
    """A finished task stuck at 99% is the bug this exception exists to
    prevent: `current >= total` always writes, throttle or not."""
    app, Session = env
    monkeypatch.setattr(progress_module, "MIN_WRITE_INTERVAL_SECONDS", 3600)

    @app.task(name="progress.completes", bind=True)
    @track()
    def completes(self):
        report_progress(1, 3, "starting")
        report_progress(2, 3, "middle")   # throttled away
        report_progress(3, 3, "finished")  # completion — must land anyway
        return "ok"

    completes.apply(task_id="p-complete")

    row = _row(Session, "p-complete")
    assert (row.progress_current, row.progress_total) == (3, 3)
    assert row.progress_message == "finished"


def test_throttle_window_expires(env, monkeypatch):
    app, Session = env
    monkeypatch.setattr(progress_module, "MIN_WRITE_INTERVAL_SECONDS", 0.01)

    @app.task(name="progress.expires", bind=True)
    @track()
    def expires(self):
        report_progress(1, 100)
        time.sleep(0.02)
        report_progress(2, 100)
        return "ok"

    expires.apply(task_id="p-expires")

    assert _row(Session, "p-expires").progress_current == 2


def test_throttle_does_not_leak_between_runs(env, monkeypatch):
    """Two runs back-to-back on the same worker thread: the second run's first
    update must not be swallowed by the first run's timestamp. (`@track`
    resets the ContextVar per run, and the stored value carries its task id so
    even the untracked path cannot inherit someone else's throttle.)"""
    app, Session = env
    monkeypatch.setattr(progress_module, "MIN_WRITE_INTERVAL_SECONDS", 3600)

    @app.task(name="progress.two_runs", bind=True)
    @track()
    def two_runs(self):
        report_progress(1, 100, "tick")
        return "ok"

    two_runs.apply(task_id="p-run-a")
    two_runs.apply(task_id="p-run-b")

    assert _row(Session, "p-run-a").progress_current == 1
    assert _row(Session, "p-run-b").progress_current == 1


# --- open-ended progress and bad arguments -----------------------------------


def test_open_ended_progress_records_no_total(env):
    """"processed 412 rows" with no denominator is legal; we must not invent
    a total for it."""
    app, Session = env

    @app.task(name="progress.open", bind=True)
    @track()
    def open_ended(self):
        report_progress(412, message="counting")
        return "ok"

    open_ended.apply(task_id="p-open")

    row = _row(Session, "p-open")
    assert row.progress_current == 412
    assert row.progress_total is None
    assert row.progress_message == "counting"


def test_a_total_of_zero_or_less_is_treated_as_open_ended(env):
    app, Session = env

    @app.task(name="progress.zero_total", bind=True)
    @track()
    def zero_total(self):
        report_progress(5, 0)
        return "ok"

    zero_total.apply(task_id="p-zero")

    row = _row(Session, "p-zero")
    assert row.progress_current == 5
    assert row.progress_total is None


def test_garbage_arguments_never_raise_and_record_nothing(env):
    """A bad argument from user code must degrade to "no progress shown",
    never to an exception inside their task."""
    app, Session = env

    class Hostile:
        def __int__(self):
            raise RuntimeError("nope")

        def __str__(self):
            raise RuntimeError("nope either")

    @app.task(name="progress.garbage", bind=True)
    @track()
    def garbage(self):
        report_progress("banana")
        report_progress(None)
        report_progress(Hostile())
        report_progress(-5, 10)
        report_progress(1, Hostile(), Hostile())
        return "survived"

    result = garbage.apply(task_id="p-garbage")

    assert result.state == "SUCCESS"
    assert result.result == "survived"
    row = _row(Session, "p-garbage")
    # Only the last call had a usable `current`; the unusable total/message
    # were dropped without taking the number down with them.
    assert row.progress_current == 1
    assert row.progress_total is None
    assert row.progress_message is None


def test_a_float_current_is_coerced(env):
    app, Session = env

    @app.task(name="progress.float", bind=True)
    @track()
    def floaty(self):
        report_progress(3.9, 10.0)
        return "ok"

    floaty.apply(task_id="p-float")

    row = _row(Session, "p-float")
    assert (row.progress_current, row.progress_total) == (3, 10)


def test_a_long_message_is_truncated_rather_than_rejected(env):
    app, Session = env

    @app.task(name="progress.long_message", bind=True)
    @track()
    def long_message(self):
        report_progress(1, 2, "x" * 5000)
        return "ok"

    long_message.apply(task_id="p-long")

    row = _row(Session, "p-long")
    assert len(row.progress_message) == progress_module.MAX_MESSAGE_CHARS
    assert row.progress_current == 1  # the numbers survived the clipping


# --- message semantics -------------------------------------------------------


def test_message_is_sticky_and_an_empty_string_clears_it(env, monkeypatch):
    """Documented behaviour: `message=None` leaves whatever is on the row (so
    a bare `report_progress(i, n)` in a loop does not blank a phase label),
    and `""` is the explicit clear."""
    app, Session = env
    monkeypatch.setattr(progress_module, "MIN_WRITE_INTERVAL_SECONDS", 0)

    seen = []

    @app.task(name="progress.message", bind=True)
    @track()
    def messages(self):
        report_progress(3, 10, "step 3")
        report_progress(5, 10)          # no message: must not blank "step 3"
        seen.append(_row(Session, "p-message").progress_message)
        report_progress(7, 10, "")      # explicit clear
        seen.append(_row(Session, "p-message").progress_message)
        return "ok"

    messages.apply(task_id="p-message")

    assert seen == ["step 3", ""]
    # ...and the counts still moved while the message stayed put.
    assert _row(Session, "p-message").progress_current == 7


# --- failure modes -----------------------------------------------------------


def test_storage_failure_does_not_raise_into_the_task_body(env, monkeypatch):
    app, Session = env

    def exploding_upsert(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(decorator_module, "_upsert_task_run", exploding_upsert)

    @app.task(name="progress.storage_down", bind=True)
    @track()
    def storage_down(self):
        report_progress(1, 2, "still going")
        return "unaffected"

    result = storage_down.apply(task_id="p-storage")

    assert result.state == "SUCCESS"
    assert result.result == "unaffected"


def test_no_configured_session_factory_does_not_raise(env, monkeypatch):
    """A worker that never called `celery_manager.setup(config)` loses
    progress, exactly as it loses pause/skip and log capture — quietly, and
    without failing the task."""
    app, Session = env
    monkeypatch.setattr(decorator_module, "_session_factory", None)

    @app.task(name="progress.unconfigured", bind=True)
    @track()
    def unconfigured(self):
        report_progress(1, 2, "into the void")
        return "ok"

    result = unconfigured.apply(task_id="p-unconfigured")

    assert result.state == "SUCCESS"
    with Session() as session:
        assert session.query(TaskRun).count() == 0


def test_called_outside_any_task_warns_once_and_returns(env, caplog):
    """Called from a shell, a test, or a helper that runs both in and out of a
    task: there is no run to attribute progress to. Say so once, don't raise,
    and don't write a row keyed by nothing."""
    app, Session = env

    with caplog.at_level("WARNING"):
        report_progress(1, 2, "nowhere")
        report_progress(2, 2, "still nowhere")

    warnings = [r for r in caplog.records if "outside any running Celery task" in r.getMessage()]
    assert len(warnings) == 1
    with Session() as session:
        assert session.query(TaskRun).count() == 0
