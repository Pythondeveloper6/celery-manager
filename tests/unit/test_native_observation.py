import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.external import ExternalUser
from celery_manager.db.models import TaskRun
from celery_manager.observation import ObservationError, RunObserver, current_run
from celery_manager.runtime import setup


def observer(tmp_path, **changes):
    config = CeleryManagerConfig(celery_app=None, db_url="sqlite:///" + str(tmp_path / "native.db"),
                                secret_key="native-test-key", **changes)
    return config, RunObserver(config)


def test_sync_outcome_context_and_duplicate_visibility_preserve_return(tmp_path):
    config, observe = observer(tmp_path, observer_metadata_keys=("integration", "token"))
    @observe.observe("native.command", operation_key="business-1", outcome=lambda code: "failed" if code else "succeeded")
    def command(code):
        assert current_run() is not None
        return code
    with observe.context(tenant_id="a", correlation_id="request-1", metadata={"integration":"voice", "token":"secret", "unapproved":"drop"}):
        assert command(0) == 0
        assert command(1) == 1
    assert current_run() is None
    rows = observe.duplicates("business-1", tenant_id="a")
    assert len(rows) == 2 and {row["status"] for row in rows} == {"succeeded", "failed"}
    assert observe.duplicates("business-1", tenant_id="b") == []
    with setup(config)() as session:
        runs = session.query(TaskRun).all()
        assert all(run.source == "external" and run.correlation_id == "request-1" for run in runs)
        assert json.loads(runs[0].context_json) == {"integration":"voice", "token":"[redacted]"}


def test_async_isolation_offloop_and_cancellation(tmp_path):
    config, observe = observer(tmp_path)
    async def scenario():
        @observe.observe("native.async")
        async def task(value):
            await asyncio.sleep(0)
            return value
        async def scoped(tenant):
            async with observe.context(tenant_id=tenant):
                return await task(tenant)
        assert await asyncio.gather(scoped("a"), scoped("b")) == ["a", "b"]
        async def cancelled():
            async with observe.observe("native.cancel", run_id="cancelled"):
                raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await cancelled()
        assert current_run() is None
    asyncio.run(scenario())
    with setup(config)() as session:
        assert session.query(TaskRun).filter_by(task_id="external:cancelled").one().status == "interrupted"
        assert {run.tenant_id for run in session.query(TaskRun).filter_by(task_name="native.async")} == {"a", "b"}


def test_lifecycle_immutable_identity_and_no_host_exception_replacement(tmp_path):
    config, observe = observer(tmp_path, observer_fail_open=False)
    handle = observe.queued("native", run_id="one", tenant_id="a")
    handle.start(); handle.success()
    with pytest.raises(ObservationError):
        observe.observe("other", run_id="one", tenant_id="b").start()
    class BodyError(Exception):
        pass
    with pytest.raises(BodyError), observe.observe("failure"):
        raise BodyError("token=do-not-store")
    with setup(config)() as session:
        assert "do-not-store" not in session.query(TaskRun).filter_by(task_name="failure").one().exception


def test_fail_open_initialization_and_concurrent_runs(tmp_path):
    _config, observe = observer(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: observe.queued("job", run_id=str(i)).success(), range(12)))
    assert observe.health()["writes_failed"] == 0
    bad = RunObserver(CeleryManagerConfig(celery_app=None, db_url="no-driver://secret@bad"))
    with bad.observe("still-runs"):
        value = 42
    assert value == 42 and bad.health()["writes_failed"] >= 1


def test_monitoring_only_capabilities_and_controls_are_explicit(tmp_path):
    config, observe = observer(tmp_path)
    config.authenticate_request = lambda _: ExternalUser("admin", "admin")
    observe.queued("native", run_id="observed").success()
    app, _ = create_app(config)
    client = TestClient(app)
    assert client.get("/api/auth/options").json()["monitoring_only"]
    assert client.get("/api/tasks").json()["total"] == 1
    for path in ("/api/tasks/start", "/api/tasks/external:observed/retry", "/api/tasks/external:observed/cancel", "/api/dead-letters/rerun"):
        assert client.post(path, json={}).status_code == 409
    assert client.get("/api/workers").status_code == 409
    assert client.get("/api/tasks/external:observed").json()["source"] == "external"


def test_monitoring_only_boot_does_not_disable_existing_schedules(tmp_path):
    from celery_manager.db.models import PeriodicSchedule
    config, _observe = observer(tmp_path)
    factory = setup(config)
    with factory() as session:
        session.add(PeriodicSchedule(name="owned-by-host", task_name="host.command", schedule_spec="daily", enabled=True))
        session.commit()
    create_app(config)
    with factory() as session:
        assert session.query(PeriodicSchedule).one().enabled is True


def test_related_attempts_cannot_group_other_tenants(tmp_path):
    config, observe = observer(tmp_path)
    config.authenticate_request = lambda _: ExternalUser("admin", "admin")
    for tenant in ("a", "b"):
        for index in range(2):
            observe.queued("job", tenant_id=tenant, operation_key="same-key", correlation_id="same-correlation").success()
    app, sessions = create_app(config)
    with sessions() as session:
        run = session.query(TaskRun).filter_by(tenant_id="a").first()
        task_id = run.task_id
    client = TestClient(app)
    for kind in ("operation", "correlation"):
        response = client.get(f"/api/tasks/{task_id}/related?kind={kind}")
        assert response.status_code == 200
        assert len(response.json()["items"]) == 2


def test_disposable_storage_gate_contract_on_sqlite(tmp_path):
    from tests.native_operability import exercise_native_operability
    config = CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///{tmp_path / 'native-contract.db'}")
    exercise_native_operability(config)


def test_active_run_inherits_explicit_identity_to_nested_runs_and_restores_outer_scope(tmp_path):
    config, observe = observer(tmp_path)
    with observe.context(tenant_id="outer", correlation_id="outer-request"):
        with (observe.observe("parent", tenant_id="inner", correlation_id="inner-request", operation_key="business"),
              observe.observe("child") as child):
            assert child.tenant_id == "inner" and child.correlation_id == "inner-request"
            assert child.operation_key == "business"
        with observe.observe("restored") as restored:
            assert restored.tenant_id == "outer" and restored.correlation_id == "outer-request"
    with setup(config)() as session:
        assert session.query(TaskRun).filter_by(task_name="child").one().tenant_id == "inner"


def test_metadata_defaults_are_allowlisted_and_bearer_exception_is_redacted(tmp_path):
    config, observe = observer(tmp_path)
    with pytest.raises(ValueError), observe.observe("failure", context={"integration":"safe", "unapproved":"do-not-store"}):
        raise ValueError("Authorization: Bearer do-not-store-this-token")
    with setup(config)() as session:
        row = session.query(TaskRun).one()
        assert json.loads(row.context_json) == {"integration":"safe"}
        assert "do-not-store-this-token" not in row.exception


def test_async_cancel_during_entry_keeps_database_offloop_and_records_interrupted(tmp_path, monkeypatch):
    import threading
    config, observe = observer(tmp_path)
    setup(config)
    handle = observe.observe("cancel-during-entry", run_id="entry-cancel")
    entered, release = threading.Event(), threading.Event()
    threads, body = [], []
    transition = handle._transition
    def slow(status, **kwargs):
        threads.append(threading.get_ident())
        if status == "started":
            entered.set()
            assert release.wait(5)
        return transition(status, **kwargs)
    monkeypatch.setattr(handle, "_transition", slow)
    async def scenario():
        async def work():
            async with handle:
                body.append("must not run")
        task = asyncio.create_task(work())
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert current_run() is None
    asyncio.run(scenario())
    assert body == [] and all(identity != threading.get_ident() for identity in threads)
    with setup(config)() as session:
        assert session.query(TaskRun).filter_by(task_id=handle.run_id).one().status == "interrupted"


def test_telemetry_exit_and_hook_errors_never_replace_original_body_exception(tmp_path, monkeypatch):
    config, observe = observer(tmp_path, observer_fail_open=False)
    def broken_callback(_health):
        raise RuntimeError("telemetry callback failure")
    config.observer_error_callback = broken_callback
    handle = observe.observe("original-exception")
    transition = handle._transition
    def broken_exit(status, **kwargs):
        if status == "failed":
            raise ConnectionError("telemetry database unavailable")
        return transition(status, **kwargs)
    monkeypatch.setattr(handle, "_transition", broken_exit)
    original = ValueError("host body failure")
    with pytest.raises(ValueError) as caught, handle:
        raise original
    assert caught.value is original
    assert observe.health()["writes_failed"] == 1
    assert current_run() is None


def test_concurrent_same_run_id_cannot_reassign_tenant(tmp_path):
    config, observe = observer(tmp_path)
    setup(config)
    def record(tenant):
        observe.queued("same-run", run_id="shared-native-id", tenant_id=tenant).success()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(record, ["a", "b"]))
    with setup(config)() as session:
        rows = session.query(TaskRun).all()
        assert len(rows) == 1 and rows[0].tenant_id in {"a", "b"}
        assert rows[0].status == "succeeded"
    assert observe.health()["writes_failed"] >= 1


def test_duplicate_visibility_defaults_to_active_tenant_context(tmp_path):
    _config, observe = observer(tmp_path)
    observe.queued("job", tenant_id=None, operation_key="same-key").success()
    observe.queued("job", tenant_id="a", operation_key="same-key").success()
    with observe.context(tenant_id="a"):
        assert {row["tenant_id"] for row in observe.duplicates("same-key")} == {"a"}
        assert {row["tenant_id"] for row in observe.duplicates("same-key", tenant_id=None)} == {None}


def test_native_skip_reason_fits_supported_database_column(tmp_path):
    config, observe = observer(tmp_path, observer_fail_open=False)
    observe.queued("long-skip", run_id="long-skip").skip("x" * 1000)
    with setup(config)() as session:
        row = session.query(TaskRun).filter_by(task_id="external:long-skip").one()
        assert row.status == "skipped" and row.skip_reason == "x" * 255


def test_database_collation_cannot_redirect_external_attempt_identity(tmp_path):
    from sqlalchemy.schema import CreateIndex, CreateTable
    config, observe = observer(tmp_path, observer_fail_open=False, auto_migrate=False)
    factory = setup(config)
    engine = factory.kw["bind"]
    ddl = str(CreateTable(TaskRun.__table__).compile(engine)).replace("task_id VARCHAR(255)", "task_id VARCHAR(255) COLLATE NOCASE")
    assert "COLLATE NOCASE" in ddl
    with engine.begin() as connection:
        connection.exec_driver_sql(ddl)
        for index in TaskRun.__table__.indexes:
            connection.execute(CreateIndex(index))
    observe.queued("same-job", run_id="Case")
    with pytest.raises(ObservationError) as failure:
        observe.observe("same-job", run_id="case").start()
    assert "identity collision" in str(failure.value.__cause__)
    with factory() as session:
        row = session.query(TaskRun).one()
        assert row.task_id == "external:Case" and row.status == "queued"
