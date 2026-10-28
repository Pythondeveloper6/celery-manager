"""Two Celery apps in one host process must never share runtime storage."""
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from celery import Celery

from celery_manager import CeleryManagerConfig, report_progress, setup, track
from celery_manager.db.models import TaskRun


def make_app(name, database, *, store_results=True, redactor=None):
    app = Celery(name, broker="memory://", backend="cache+memory://")
    config = CeleryManagerConfig(celery_app=app, db_url=f"sqlite:///{database}", store_results=store_results,
                                log_redactor=redactor)
    sessions = setup(config)
    @app.task(name=f"{name}.run", shared=False)
    @track()
    def task(value):
        logging.getLogger("multiapp").warning("secret-%s", value)
        report_progress(1, 1, "done")
        return value
    return app, config, sessions, task


def test_eager_apps_use_their_own_storage_and_result_log_policies(tmp_path):
    _app_a, config_a, sessions_a, task_a = make_app("isolated_a", tmp_path / "a.db", store_results=False,
                                                  redactor=lambda message: message.replace("secret", "REDACTED"))
    _app_b, _, sessions_b, task_b = make_app("isolated_b", tmp_path / "b.db")
    # B was configured last, but A must still use its own database and policy.
    assert task_a.apply(args=["a"], task_id="same-id").get() == "a"
    assert task_b.apply(args=["b"], task_id="same-id").get() == "b"
    assert setup(config_a) is sessions_a
    with sessions_a() as session:
        row = session.query(TaskRun).one()
        assert row.task_name == "isolated_a.run" and row.result_repr is None
        assert "REDACTED-a" in row.log_output and "secret" not in row.log_output
        assert row.progress_current == 1
    with sessions_b() as session:
        row = session.query(TaskRun).one()
        assert row.task_name == "isolated_b.run" and row.result_repr == '"b"'
        assert "secret-b" in row.log_output and row.progress_current == 1


def test_concurrent_apps_do_not_swap_contexts_or_capture_each_others_logs(tmp_path):
    a = make_app("parallel_a", tmp_path / "a.db", redactor=lambda text: text.replace("secret", "A"))
    b = make_app("parallel_b", tmp_path / "b.db", redactor=lambda text: text.replace("secret", "B"))
    barrier = Barrier(2)
    def execute(env, value):
        barrier.wait(timeout=10)
        env[3].apply(args=[value], task_id="parallel-id").get()
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda pair: execute(*pair), [(a, "first"), (b, "second")]))
    for env, own, foreign in [(a, "A-first", "second"), (b, "B-second", "first")]:
        with env[2]() as session:
            row = session.query(TaskRun).one()
            assert own in row.log_output and foreign not in row.log_output
            assert row.progress_current == 1


def test_unconfigured_app_does_not_leak_into_last_configured_database(tmp_path):
    _, _, sessions, _ = make_app("configured", tmp_path / "configured.db")
    other = Celery("unconfigured", broker="memory://", backend="cache+memory://")
    @other.task(name="unconfigured.run", shared=False)
    @track()
    def task():
        return "untouched"
    assert task.apply(task_id="must-not-leak").get() == "untouched"
    with sessions() as session:
        assert session.query(TaskRun).count() == 0


def test_undecorated_progress_resolves_owning_app(tmp_path):
    a = make_app("progress_a", tmp_path / "a.db")
    b = make_app("progress_b", tmp_path / "b.db")
    def attach(app, name):
        @app.task(name=name, shared=False)
        def task():
            report_progress(2, 2)
            return "done"
        return task
    task_a = attach(a[0], "plain_a")
    task_b = attach(b[0], "plain_b")
    task_a.apply(task_id="plain-same").get()
    task_b.apply(task_id="plain-same").get()
    for env, name in [(a, "plain_a"), (b, "plain_b")]:
        with env[2]() as session:
            row = session.query(TaskRun).one()
            assert row.task_name == name and row.progress_current == 2


def test_occurrence_stamping_cache_is_isolated_per_app(tmp_path):
    from datetime import datetime, timezone

    from celery_manager.db.models import ScheduleOverride
    a = make_app("stamped_a", tmp_path / "a.db")
    b = make_app("legacy_b", tmp_path / "b.db")
    now = datetime.now(timezone.utc)
    a[3].apply(args=["stamped"], task_id="stamped", headers={"cm_schedule": "a-schedule", "cm_occurrence": now.isoformat()}).get()
    with b[2]() as session:
        session.add(ScheduleOverride(schedule_name="legacy_b.run", target_time=now, action="skip", consumed=False, created_by="test"))
        session.commit()
    # A's stamped traffic must not disable B's explicitly supported legacy matching.
    assert b[3].apply(args=["skipped"], task_id="legacy").get() is None
    with b[2]() as session:
        assert session.query(TaskRun).one().status == "skipped"
        assert session.query(ScheduleOverride).one().consumed is True


def test_nested_apps_restore_outer_task_runtime(tmp_path):
    a = make_app("outer_app", tmp_path / "a.db")
    b = make_app("inner_app", tmp_path / "b.db")
    @a[0].task(name="outer.nested", shared=False)
    @track()
    def outer():
        b[3].apply(args=["inner"], task_id="inner").get()
        report_progress(3, 3, "outer done")
        return "outer result"
    assert outer.apply(task_id="outer").get() == "outer result"
    with a[2]() as session:
        row = session.query(TaskRun).one()
        assert row.task_id == "outer" and row.progress_current == 3
    with b[2]() as session:
        assert session.query(TaskRun).one().task_id == "inner"


def test_public_legacy_configure_after_app_setup_is_explicit_process_override(tmp_path):
    from celery_manager.db.session import get_session_factory, init_db
    from celery_manager.decorator import configure
    _app, _, original, task = make_app("before_legacy", tmp_path / "original.db")
    engine = init_db(CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///{tmp_path / 'legacy.db'}"))
    legacy = get_session_factory(engine)
    try:
        configure(legacy)
        assert task.apply(args=["legacy"], task_id="legacy-configure").get() == "legacy"
        with legacy() as session:
            assert session.query(TaskRun).one().task_id == "legacy-configure"
        with original() as session:
            assert session.query(TaskRun).count() == 0
    finally:
        engine.dispose()
