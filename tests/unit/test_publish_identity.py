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
