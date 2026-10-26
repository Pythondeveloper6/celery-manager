from celery import Celery
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import workflows
from celery_manager.db.models import Base, TaskRun
from celery_manager.decorator import configure, track
from celery_manager.events.listener import upsert_task_run


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


# --- listener -------------------------------------------------------------


def test_listener_captures_root_and_parent_from_task_sent():
    session = _session()
    upsert_task_run(session, {
        "type": "task-sent", "uuid": "c", "name": "x", "root_id": "r", "parent_id": "p",
    })
    session.commit()
    run = session.query(TaskRun).filter_by(task_id="c").one()
    assert (run.root_id, run.parent_id, run.group_id) == ("r", "p", None)


def test_later_events_do_not_null_known_lineage():
    session = _session()
    upsert_task_run(session, {"type": "task-received", "uuid": "c", "name": "x", "root_id": "r", "parent_id": "p"})
    upsert_task_run(session, {"type": "task-started", "uuid": "c"})
    upsert_task_run(session, {"type": "task-succeeded", "uuid": "c", "root_id": None, "parent_id": None})
    session.commit()
    run = session.query(TaskRun).filter_by(task_id="c").one()
    assert (run.root_id, run.parent_id) == ("r", "p")
    assert run.status == "succeeded"


# --- decorator ------------------------------------------------------------


def _decorated_env(name):
    app = Celery(name, broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    configure(session_factory=Session, eta_tolerance_seconds=60)

    @app.task(name=f"{name}.step", bind=True)
    @track()
    def step(self):
        return 1

    return Session, step


def test_decorator_records_lineage_from_request():
    Session, step = _decorated_env("wf1")
    # Eager `apply` derives lineage itself, so install the request a worker
    # would have built for a chain/group member and call the wrapped body.
    step.push_request(id="child", root_id="root", parent_id="par", group="grp")
    step.run()
    with Session() as session:
        run = session.query(TaskRun).filter_by(task_id="child").one()
        assert run.root_id == "root"
        assert run.parent_id == "par"
        assert run.group_id == "grp"


def test_decorator_does_not_null_listener_lineage():
    Session, step = _decorated_env("wf2")
    with Session() as session:
        upsert_task_run(session, {"type": "task-sent", "uuid": "k", "name": "wf2.step", "root_id": "R", "parent_id": "P"})
        session.commit()
    step.push_request(id="k")  # no lineage on the request: keep what the listener stored
    step.run()
    with Session() as session:
        run = session.query(TaskRun).filter_by(task_id="k").one()
        assert (run.root_id, run.parent_id) == ("R", "P")


# --- endpoint -------------------------------------------------------------


def _client():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, factory = create_app(config)
    with factory() as session:
        create_user(session, "alice", "pw")
        session.commit()
    return TestClient(app, base_url="https://testserver"), factory


def _add(factory, task_id, parent=None, root=None, group=None, status="succeeded"):
    with factory() as session:
        session.add(TaskRun(
            task_id=task_id, task_name=f"demo.{task_id}", status=status,
            parent_id=parent, root_id=root, group_id=group,
        ))
        session.commit()


def _login(client):
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})


def test_workflow_requires_auth():
    client, _ = _client()
    assert client.get("/api/tasks/a/workflow").status_code == 401


def test_workflow_unknown_id_is_404():
    client, _ = _client()
    _login(client)
    assert client.get("/api/tasks/nope/workflow").status_code == 404


def test_workflow_builds_the_tree_from_any_member():
    client, factory = _client()
    _login(client)
    _add(factory, "a")  # chain root, no root_id of its own
    _add(factory, "b", parent="a", root="a")
    _add(factory, "g1", parent="b", root="a", group="G")
    _add(factory, "g2", parent="b", root="a", group="G", status="failed")
    _add(factory, "cb", parent="b", root="a")
    _add(factory, "other", root="zzz")
    for start in ("a", "g2", "cb"):
        body = client.get(f"/api/tasks/{start}/workflow").json()
        assert body["root_id"] == "a"
        assert body["current"] == start
        assert {n["task_id"] for n in body["nodes"]} == {"a", "b", "g1", "g2", "cb"}
        assert body["truncated"] is False
        assert {"from": "b", "to": "g1"} in body["edges"]
        assert {"from": "a", "to": "b"} in body["edges"]
        assert len(body["edges"]) == 4
    node = {n["task_id"]: n for n in body["nodes"]}["g1"]
    assert node["group_id"] == "G" and node["parent_id"] == "b"


def test_standalone_run_is_a_tree_of_one():
    client, factory = _client()
    _login(client)
    _add(factory, "solo")
    body = client.get("/api/tasks/solo/workflow").json()
    assert [n["task_id"] for n in body["nodes"]] == ["solo"]
    assert body["edges"] == []


def test_workflow_caps_nodes_and_flags_truncation(monkeypatch):
    client, factory = _client()
    _login(client)
    monkeypatch.setattr(workflows, "MAX_WORKFLOW_NODES", 3)
    _add(factory, "r")
    for i in range(6):
        _add(factory, f"c{i}", parent="r", root="r")
    body = client.get("/api/tasks/r/workflow").json()
    assert len(body["nodes"]) == 3
    assert body["truncated"] is True
    # The requested run is kept even when it falls outside the cap.
    late = client.get("/api/tasks/c5/workflow").json()
    assert "c5" in {n["task_id"] for n in late["nodes"]}


def test_task_detail_exposes_lineage():
    client, factory = _client()
    _login(client)
    _add(factory, "x", parent="p", root="r", group="g")
    body = client.get("/api/tasks/x").json()
    assert (body["parent_id"], body["root_id"], body["group_id"]) == ("p", "r", "g")
