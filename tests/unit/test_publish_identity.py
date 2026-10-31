from celery import Celery, current_app

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import TaskRun
from celery_manager.publish import install_publish_hook


def test_worker_published_children_inherit_parent_actor_but_explicit_actor_wins():
    previous = current_app._get_current_object()
    app = Celery("identity-workflow", broker="memory://", backend="cache+memory://")
    _, sessions = create_app(CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="test-secret"))
    app.set_current()

    @app.task(name="identity.child", shared=False)
    def child():
        return None

    @app.task(name="identity.parent", shared=False)
    def parent():
        return [child.apply_async().id, child.apply_async(headers={"cm_initiator": "explicit"}).id]

    uninstall = install_publish_hook(app, session_factory=sessions, initiator=lambda: "callback")
    try:
        ids = parent.apply(headers={"cm_initiator": "issuer:parent"}).get()
        with sessions() as session:
            assert session.query(TaskRun).filter_by(task_id=ids[0]).one().initiator == "issuer:parent"
            assert session.query(TaskRun).filter_by(task_id=ids[1]).one().initiator == "explicit"
    finally:
        uninstall()
        app.close()
        previous.set_current()


def test_trusted_context_flows_to_celery_children_and_related_runs():
    previous = current_app._get_current_object()
    app = Celery("context-workflow", broker="memory://", backend="cache+memory://")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="test-secret")
    _, sessions = create_app(config)
    app.set_current()
    @app.task(name="context.child", shared=False)
    def child(): return None
    @app.task(name="context.parent", shared=False)
    def parent(): return child.apply_async().id
    uninstall = install_publish_hook(app, session_factory=sessions)
    try:
        context = {"tenant_id":"tenant-a", "correlation_id":"request-1", "operation_key":"business-1", "metadata":{"integration":"voice", "password":"hidden"}}
        parent_id = parent.apply_async(headers={"cm_context": context}).id
        child_id = parent.apply(headers={"cm_context": context}).get()
        with sessions() as session:
            for identifier in (parent_id, child_id):
                row = session.query(TaskRun).filter_by(task_id=identifier).one()
                assert row.source == "celery" and row.tenant_id == "tenant-a"
                assert row.correlation_id == "request-1" and row.operation_key == "business-1"
                assert "hidden" not in row.context_json
    finally:
        uninstall(); app.close(); previous.set_current()


def test_publish_context_identity_cannot_rebind_previously_null_identity():
    from celery import signals
    previous = current_app._get_current_object()
    app = Celery("immutable-context", broker="memory://", backend="cache+memory://")
    _, sessions = create_app(CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="test-secret"))
    app.set_current()
    uninstall = install_publish_hook(app, session_factory=sessions)
    try:
        def send(context):
            signals.after_task_publish.send(sender="context.task", headers={"id":"same-context-id", "cm_context":context}, body=([], {}, {}))
        send({"metadata": {"integration":"original"}})
        send({"tenant_id":"other-tenant", "correlation_id":"other-request", "operation_key":"other-op", "metadata":{}})
        with sessions() as session:
            row = session.query(TaskRun).one()
            assert row.tenant_id is None and row.correlation_id is None and row.operation_key is None
    finally:
        uninstall(); app.close(); previous.set_current()


def test_active_native_run_context_reaches_celery_publish():
    from celery_manager import RunObserver
    previous = current_app._get_current_object()
    app = Celery("native-to-celery-context", broker="memory://", backend="cache+memory://")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="test-secret")
    _, sessions = create_app(config)
    app.set_current()
    @app.task(name="native.context.child", shared=False)
    def child(): return None
    uninstall = install_publish_hook(app, session_factory=sessions)
    try:
        with RunObserver(config).observe("host.action", tenant_id="a", correlation_id="trace", operation_key="op"):
            identifier = child.apply_async().id
        with sessions() as session:
            row = session.query(TaskRun).filter_by(task_id=identifier).one()
            assert (row.tenant_id, row.correlation_id, row.operation_key) == ("a", "trace", "op")
    finally:
        uninstall(); app.close(); previous.set_current()


def test_context_helpers_refuse_invalid_storage_and_preserve_initialized_nulls():
    from types import SimpleNamespace

    import pytest

    from celery_manager.run_context import (
        context_for_run,
        merge_storage_context,
        normalize_context,
    )
    row = SimpleNamespace(source="celery", tenant_id=None, correlation_id=None, operation_key=None, context_json=None)
    assert context_for_run(row) is None
    merge_storage_context(row, normalize_context({"metadata": {"integration":"safe"}}))
    assert context_for_run(row)["metadata"] == {"integration":"safe"}
    with pytest.raises(ValueError, match="identity collision"):
        merge_storage_context(row, normalize_context({"tenant_id":"reassigned"}))
    assert row.tenant_id is None
    for invalid in ("broken JSON", "[]", '"scalar"'):
        row.context_json = invalid
        with pytest.raises(ValueError):
            context_for_run(row)
