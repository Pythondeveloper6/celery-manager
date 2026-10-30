"""Role-based access: viewer / operator / admin.

Every state-changing route declares a minimum role, the server enforces it
(403 below it), the app refuses to start with a route that declares none,
existing accounts become admin on upgrade, and the CLI manages roles."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest
from alembic import command
from celery import Celery
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from celery_manager import cli
from celery_manager.api.app import (
    PUBLIC_WRITE_ROUTES,
    create_app,
    iter_api_routes,
    route_required_role,
    unclassified_write_routes,
)
from celery_manager.api.deps import require_role
from celery_manager.api.routes import deadletters as deadletter_routes
from celery_manager.api.routes import delayed as delayed_routes
from celery_manager.api.routes import maintenance as maintenance_routes
from celery_manager.api.routes import queues as queue_routes
from celery_manager.api.routes import rabbitmq as rabbitmq_routes
from celery_manager.api.routes import schedules as schedule_routes
from celery_manager.api.routes import tasks as task_routes
from celery_manager.api.routes import unacked as unacked_routes
from celery_manager.api.routes import workers as worker_routes
from celery_manager.auth.roles import ROLES, role_allows, validate_role
from celery_manager.auth.users import create_user, get_user_role, set_user_role
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.migrate import alembic_config, upgrade_database
from celery_manager.db.models import AuditLog, User

# --- the matrix -------------------------------------------------------------

# (method, path, json body, minimum role). One entry per state-changing route;
# `test_matrix_covers_every_state_changing_route` keeps it complete.
WRITE_ROUTES = [
    ("DELETE", "/api/health/schedulers/missing", None, "admin"),
    ("POST", "/api/operations/annotations", {"kind":"deployment","label":"test"}, "admin"),
    ("DELETE", "/api/health/beat/owner?owner_id=missing", None, "admin"),
    ("POST", "/api/auth/sessions/missing/revoke", None, "viewer"),
    ("POST", "/api/dead-letters/t1/reopen", {}, "operator"),
    ("POST", "/api/dead-letters/t1/ignore", {}, "operator"),
    ("POST", "/api/dead-letters/t1/acknowledge", {}, "operator"),
    ("POST", "/api/schedule-reconciliation/apply", {}, "admin"),
    ("POST", "/api/schedule-reconciliation/plan", {}, "admin"),
    ("POST", "/api/schedules/pause-import", {"csv":"schedule_name,paused_until\n"}, "admin"),
    ("POST", "/api/locks/release", {"key":"x", "lease_id":"x"}, "admin"),
    ("POST", "/api/auth/bridge", None, "viewer"),
    ("POST", "/api/schedules", {"name":"example", "task":"demo.echo", "schedule":{"type":"interval","seconds":60}}, "admin"),
    ("PATCH", "/api/schedules/nightly", {"enabled":False}, "operator"),
    ("DELETE", "/api/schedules/nightly", None, "admin"),
    ("POST", "/api/schedules/preview", {"type":"interval","seconds":60}, "viewer"),
    ("POST", "/api/schedules/nightly/skip-next", None, "operator"),
    ("POST", "/api/schedules/nightly/run-now", None, "operator"),
    ("POST", "/api/tasks/start", {"task_name": "demo.echo"}, "operator"),
    ("POST", "/api/tasks/t1/cancel", None, "operator"),
    ("POST", "/api/tasks/t1/retry", None, "operator"),
    ("POST", "/api/tasks/retry-bulk", {"task_ids": ["t1"]}, "operator"),
    ("POST", "/api/tasks/cancel-bulk", {"task_ids": ["t1"]}, "operator"),
    ("POST", "/api/dead-letters/rerun", {"task_ids": ["t1"]}, "operator"),
    (
        "POST",
        "/api/schedules/nightly/occurrences/pause",
        {"target_time": "2030-01-01T00:00:00Z", "action": "skip"},
        "operator",
    ),
    ("POST", "/api/schedules/nightly/pause", None, "operator"),
    ("POST", "/api/schedules/nightly/resume", None, "operator"),
    ("POST", "/api/schedules/nightly/preview", {"type": "interval", "seconds": 60}, "viewer"),
    ("PUT", "/api/schedules/nightly/schedule", {"type": "interval", "seconds": 60}, "operator"),
    ("DELETE", "/api/schedules/nightly/schedule", None, "operator"),
    (
        "POST",
        "/api/maintenance-windows",
        {"name": "w", "days_of_week": [6], "start_time": "02:00", "duration_minutes": 120},
        "operator",
    ),
    ("PUT", "/api/maintenance-windows/1", {"name": "w"}, "operator"),
    ("DELETE", "/api/maintenance-windows/1", None, "operator"),
    (
        "POST",
        "/api/maintenance-windows/preview",
        {"days_of_week": [6], "start_time": "02:00", "duration_minutes": 120},
        "viewer",
    ),
    ("POST", "/api/workers/w1/pool/grow", None, "operator"),
    ("POST", "/api/workers/w1/pool/shrink", None, "operator"),
    ("POST", "/api/workers/w1/queues/add", {"queue": "q"}, "operator"),
    ("POST", "/api/workers/w1/queues/cancel", {"queue": "q"}, "operator"),
    (
        "POST",
        "/api/tasks/rate-limit",
        {"task_name": "demo.echo", "rate_limit": "10/m"},
        "operator",
    ),
    ("POST", "/api/delayed/cancel", {"task_ids": ["t1"]}, "operator"),
    ("POST", "/api/queues/unacked/restore", {"tags": ["tag-1"]}, "operator"),
    ("POST", "/api/workers/w1/shutdown", None, "admin"),
    ("POST", "/api/queues/celery/purge", None, "admin"),
    ("DELETE", "/api/queues/celery/messages/t1", None, "admin"),
    ("POST", "/api/queues/celery/messages/t1/move", {"target": "other"}, "admin"),
    ("POST", "/api/rabbitmq/dlqs/celery.dlq/requeue", {}, "admin"),
    ("PUT", "/api/settings", {"brand_name": "Acme"}, "admin"),
    ("PUT", "/api/settings/retention", {"default_days": None}, "admin"),
    ("POST", "/api/settings/retention/preview", {}, "viewer"),
    (
        "POST",
        "/api/users",
        {"username": "newbie", "password": "long-enough-pw", "role": "viewer"},
        "admin",
    ),
    ("PATCH", "/api/users/viewer", {"role": "viewer"}, "admin"),
    ("POST", "/api/users/ghost/password", {"generate": True}, "admin"),
    ("DELETE", "/api/users/ghost", None, "admin"),
    # Alerts: an unknown kind (422) or made-up ids (404), so an allowed role
    # gets past the check without creating anything.
    ("POST", "/api/alerts/rules", {"name": "r", "kind": "bogus"}, "admin"),
    ("PATCH", "/api/alerts/rules/999", {"enabled": False}, "admin"),
    ("DELETE", "/api/alerts/rules/999", None, "admin"),
    ("POST", "/api/alerts/channels", {"name": "c", "kind": "bogus"}, "admin"),
    ("PATCH", "/api/alerts/channels/999", {"name": "x"}, "admin"),
    ("DELETE", "/api/alerts/channels/999", None, "admin"),
    ("POST", "/api/alerts/channels/999/test", None, "admin"),
    # A too-short new password: past the role check (so allowed for every
    # role) without changing anything.
    (
        "POST",
        "/api/me/password",
        {"current_password": "pw", "new_password": "short"},
        "viewer",
    ),
]

# The same matrix by route template, as the app declares it.
ROUTE_ROLES = {
    ("DELETE", "/api/health/schedulers/{instance_id}"): "admin",
    ("POST", "/api/operations/annotations"): "admin",
    ("DELETE", "/api/health/beat/owner"): "admin",
    ("POST", "/api/auth/sessions/{session_id}/revoke"): "viewer",
    ("POST", "/api/dead-letters/{task_id}/reopen"): "operator",
    ("POST", "/api/dead-letters/{task_id}/ignore"): "operator",
    ("POST", "/api/dead-letters/{task_id}/acknowledge"): "operator",
    ("POST", "/api/schedule-reconciliation/apply"): "admin",
    ("POST", "/api/schedule-reconciliation/plan"): "admin",
    ("POST", "/api/schedules/pause-import"): "admin",
    ("POST", "/api/locks/release"): "admin",
    ("POST", "/api/auth/bridge"): "viewer",
    ("POST", "/api/schedules"): "admin",
    ("PATCH", "/api/schedules/{name}"): "operator",
    ("DELETE", "/api/schedules/{name}"): "admin",
    ("POST", "/api/schedules/preview"): "viewer",
    ("POST", "/api/schedules/{name}/skip-next"): "operator",
    ("POST", "/api/schedules/{name}/run-now"): "operator",
    ("POST", "/api/tasks/start"): "operator",
    ("POST", "/api/tasks/{task_id}/cancel"): "operator",
    ("POST", "/api/tasks/{task_id}/retry"): "operator",
    ("POST", "/api/tasks/retry-bulk"): "operator",
    ("POST", "/api/tasks/cancel-bulk"): "operator",
    ("POST", "/api/dead-letters/rerun"): "operator",
    ("POST", "/api/schedules/{name}/occurrences/pause"): "operator",
    ("POST", "/api/schedules/{name}/pause"): "operator",
    ("POST", "/api/schedules/{name}/resume"): "operator",
    ("POST", "/api/schedules/{name}/preview"): "viewer",
    ("PUT", "/api/schedules/{name}/schedule"): "operator",
    ("DELETE", "/api/schedules/{name}/schedule"): "operator",
    ("POST", "/api/maintenance-windows"): "operator",
    ("PUT", "/api/maintenance-windows/{window_id}"): "operator",
    ("DELETE", "/api/maintenance-windows/{window_id}"): "operator",
    ("POST", "/api/maintenance-windows/preview"): "viewer",
    ("POST", "/api/workers/{worker}/pool/grow"): "operator",
    ("POST", "/api/workers/{worker}/pool/shrink"): "operator",
    ("POST", "/api/workers/{worker}/queues/add"): "operator",
    ("POST", "/api/workers/{worker}/queues/cancel"): "operator",
    ("POST", "/api/tasks/rate-limit"): "operator",
    ("POST", "/api/delayed/cancel"): "operator",
    ("POST", "/api/queues/unacked/restore"): "operator",
    ("POST", "/api/workers/{worker}/shutdown"): "admin",
    ("POST", "/api/queues/{name}/purge"): "admin",
    ("DELETE", "/api/queues/{name}/messages/{task_id}"): "admin",
    ("POST", "/api/queues/{name}/messages/{task_id}/move"): "admin",
    ("POST", "/api/rabbitmq/dlqs/{name}/requeue"): "admin",
    ("PUT", "/api/settings"): "admin",
    ("PUT", "/api/settings/retention"): "admin",
    ("POST", "/api/settings/retention/preview"): "viewer",
    ("POST", "/api/users"): "admin",
    ("PATCH", "/api/users/{target}"): "admin",
    ("POST", "/api/users/{target}/password"): "admin",
    ("DELETE", "/api/users/{target}"): "admin",
    ("POST", "/api/me/password"): "viewer",
    ("POST", "/api/alerts/rules"): "admin",
    ("PATCH", "/api/alerts/rules/{rule_id}"): "admin",
    ("DELETE", "/api/alerts/rules/{rule_id}"): "admin",
    ("POST", "/api/alerts/channels"): "admin",
    ("PATCH", "/api/alerts/channels/{channel_id}"): "admin",
    ("DELETE", "/api/alerts/channels/{channel_id}"): "admin",
    ("POST", "/api/alerts/channels/{channel_id}/test"): "admin",
}

READ_ROUTES = [
    "/api/whoami",
    "/api/tasks",
    "/api/tasks/t1",
    "/api/tasks/t1/workflow",
    "/api/tasks/export?format=csv",
    "/api/dead-letters",
    "/api/schedules",
    "/api/maintenance-windows",
    "/api/audit",
    "/api/settings/retention",
    "/api/analytics/overview",
    "/api/analytics/failures",
    "/api/alerts/rules",
    "/api/alerts/channels",
    "/api/alerts/incidents",
    "/api/queues/celery/history",
    "/api/rabbitmq/overview",
    "/api/rabbitmq/dlqs",
    "/api/rabbitmq/dlqs/celery.dlq/messages",
]

# Control functions the write routes call; stubbed so an allowed request
# reaches the route body without needing a broker.
_STUBBED = {
    task_routes: [
        "start_task",
        "cancel_task",
        "retry_task",
        "bulk_retry",
        "bulk_cancel",
    ],
    deadletter_routes: ["rerun_with_arguments"],
    schedule_routes: [
        "pause_occurrence",
        "pause_schedule",
        "resume_schedule",
        "preview_schedule",
        "edit_schedule",
        "revert_schedule",
    ],
    maintenance_routes: ["create_window", "update_window", "delete_window"],
    worker_routes: ["shutdown_worker", "resize_pool", "set_consumer", "set_rate_limit"],
    queue_routes: ["purge_queue", "delete_message", "move_message"],
    delayed_routes: ["cancel_delayed_tasks"],
    unacked_routes: ["restore_unacked"],
    rabbitmq_routes: ["requeue_dlq"],
}


@pytest.fixture
def make_app():
    def build():
        config = CeleryManagerConfig(
            celery_app=Celery("test"),
            db_url="sqlite:///:memory:",
            secret_key="test-secret",
        )
        app, session_factory = create_app(config)
        with session_factory() as session:
            for role in ROLES:
                create_user(session, role, "pw", role=role)
        return app, session_factory

    return build


@pytest.fixture(scope="module")
def shared():
    """One app and a signed-in client per role, for the matrix tests: each
    account costs a bcrypt hash and a login, too slow to repeat ~100 times.
    The write routes' control functions are stubbed, so they change nothing."""
    config = CeleryManagerConfig(
        celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret"
    )
    app, session_factory = create_app(config)
    with session_factory() as session:
        for role in ROLES:
            create_user(session, role, "pw", role=role)
    return {role: _client(app, role) for role in ROLES}


def _client(app, username: str) -> TestClient:
    client = TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    )
    assert (
        client.post(
            "/api/auth/login", json={"username": username, "password": "pw"}
        ).status_code
        == 200
    )
    return client


@pytest.fixture
def stubbed(monkeypatch):
    mocks = {}
    for module, names in _STUBBED.items():
        for name in names:
            mock = MagicMock(return_value={})
            monkeypatch.setattr(module, name, mock)
            mocks[name] = mock
    return mocks


def _send(client, method, path, body):
    return (
        client.request(method, path, json=body)
        if body is not None
        else client.request(method, path)
    )


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(("method", "path", "body", "required"), WRITE_ROUTES)
def test_write_routes_enforce_their_role(
    shared, stubbed, role, method, path, body, required
):
    response = _send(shared[role], method, path, body)
    if role_allows(role, required):
        # Past the role check: whatever the route then does (200, 404 for
        # the made-up ids, a stub's {} failing serialisation), it isn't 401/403.
        assert response.status_code not in (401, 403), response.text
    else:
        assert response.status_code == 403
        assert required in response.json()["detail"]


@pytest.mark.parametrize("role", ROLES)
def test_refused_actions_never_reach_the_control_layer(shared, stubbed, role):
    client = shared[role]
    for method, path, body, required in WRITE_ROUTES:
        if not role_allows(role, required):
            _send(client, method, path, body)
    called = [name for name, mock in stubbed.items() if mock.called]
    # Viewers reach nothing; operators never reach the admin-only controls.
    if role == "viewer":
        assert called == []
    elif role == "operator":
        assert not {"shutdown_worker", "purge_queue", "delete_message", "move_message"} & set(called)
        assert "requeue_dlq" not in called


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("path", READ_ROUTES)
def test_every_role_can_read(shared, role, path):
    response = shared[role].get(path)
    assert response.status_code in (200, 404), (
        path,
        response.status_code,
        response.text,
    )


def test_writes_still_require_a_session(make_app):
    app, _ = make_app()
    client = TestClient(app, base_url="https://testserver")
    for method, path, body, _required in WRITE_ROUTES:
        assert _send(client, method, path, body).status_code == 401, path


def test_settings_change_by_admin_is_audited_as_the_admin(make_app):
    app, session_factory = make_app()
    assert (
        _client(app, "admin")
        .put("/api/settings", json={"brand_name": "Acme"})
        .status_code
        == 200
    )
    with session_factory() as session:
        entry = session.query(AuditLog).order_by(AuditLog.id.desc()).first()
        assert entry.username == "admin"


def test_operator_actions_are_audited_as_the_operator(make_app):
    app, session_factory = make_app()
    app.state.cm_config.celery_app.conf.beat_schedule = {}
    with session_factory() as session:
        from celery_manager.db.models import PeriodicSchedule

        session.add(
            PeriodicSchedule(name="nightly", task_name="demo.echo", schedule_spec="1d")
        )
        session.commit()
    assert (
        _client(app, "operator").post("/api/schedules/nightly/pause").status_code == 200
    )
    with session_factory() as session:
        entry = session.query(AuditLog).order_by(AuditLog.id.desc()).first()
        assert entry.username == "operator"


# --- whoami / login ---------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_whoami_and_login_return_the_role(make_app, role):
    app, _ = make_app()
    client = TestClient(app, base_url="https://testserver")
    login = client.post("/api/auth/login", json={"username": role, "password": "pw"})
    assert login.json() == {"username": role, "role": role, "source": "local"}
    assert client.get("/api/whoami").json() == {"username": role, "role": role, "source": "local"}


def test_role_change_applies_on_the_next_request(make_app):
    app, session_factory = make_app()
    client = _client(app, "admin")
    assert client.put("/api/settings", json={"brand_name": "One"}).status_code == 200
    with session_factory() as session:
        set_user_role(session, "admin", "viewer")
    assert client.put("/api/settings", json={"brand_name": "Two"}).status_code == 403
    assert client.get("/api/whoami").json()["role"] == "viewer"


def test_an_unknown_role_in_the_database_can_read_but_not_act(make_app, stubbed):
    app, session_factory = make_app()
    with session_factory() as session:
        session.execute(
            text("UPDATE users SET role = 'superuser' WHERE username = 'viewer'")
        )
        session.commit()
    client = _client(app, "viewer")
    assert client.get("/api/tasks").status_code == 200
    assert client.post("/api/tasks/t1/cancel").status_code == 403
    assert client.put("/api/settings", json={"brand_name": "x"}).status_code == 403


# --- fail closed: every state-changing route declares a role ----------------


def test_every_state_changing_route_declares_a_role(make_app):
    app, _ = make_app()
    assert unclassified_write_routes(app) == []


def test_matrix_covers_every_state_changing_route(make_app):
    """The role-to-route matrix, exactly. If this fails, a write route was
    added or reclassified: update ROUTE_ROLES, WRITE_ROUTES and docs/roles.md."""
    app, _ = make_app()
    declared = {}
    for path, route, dependencies in iter_api_routes(app.routes):
        for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
            if (method, path) not in PUBLIC_WRITE_ROUTES:
                declared[(method, path)] = route_required_role(route, dependencies)
    expected = {**ROUTE_ROLES, **{(method, path.replace("/api/", "/api/v1/", 1)): role
                                 for (method, path), role in ROUTE_ROLES.items()}}
    assert declared == expected
    assert len(WRITE_ROUTES) == len(ROUTE_ROLES)


def test_unclassified_route_is_detected():
    app = FastAPI()

    @app.post("/api/naked")
    def naked():
        return {}

    @app.post("/api/guarded")
    def guarded(username: str = Depends(require_role("operator"))):
        return {}

    router = APIRouter(prefix="/api/nested")

    @router.delete("/thing")
    def nested():
        return {}

    @router.put("/guarded-at-include")
    def guarded_at_include():
        return {}

    inner = APIRouter()
    inner.include_router(router)
    app.include_router(inner, prefix="/v2")

    assert unclassified_write_routes(app) == [
        ("POST", "/api/naked"),
        ("DELETE", "/v2/api/nested/thing"),
        ("PUT", "/v2/api/nested/guarded-at-include"),
    ]

    # A role declared where a router is included counts too.
    app2 = FastAPI()
    app2.include_router(router, dependencies=[Depends(require_role("admin"))])
    assert unclassified_write_routes(app2) == []


def test_create_app_refuses_an_unclassified_write_route(monkeypatch):
    from celery_manager.api.routes import workflows

    router = APIRouter(prefix="/api/tasks")

    @router.delete("/{task_id}")
    def delete_everything(task_id: str):
        return {}

    monkeypatch.setattr(workflows, "router", router)
    config = CeleryManagerConfig(
        celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret"
    )
    with pytest.raises(RuntimeError, match="DELETE /api/tasks/{task_id}"):
        create_app(config)


# --- roles and accounts -----------------------------------------------------


def test_validate_role_rejects_unknown_roles():
    assert validate_role("operator") == "operator"
    with pytest.raises(ValueError, match="unknown role"):
        validate_role("root")
    with pytest.raises(ValueError):
        require_role("root")


def test_role_ordering():
    assert role_allows("admin", "operator") and role_allows("operator", "operator")
    assert not role_allows("viewer", "operator") and not role_allows(
        "operator", "admin"
    )
    assert not role_allows(None, "viewer") and not role_allows("bogus", "viewer")


def test_create_user_defaults_to_admin_for_backwards_compatibility(make_app):
    _, session_factory = make_app()
    with session_factory() as session:
        assert create_user(session, "legacy", "pw").role == "admin"
        with pytest.raises(ValueError):
            create_user(session, "bad", "pw", role="root")


def test_set_user_role_on_a_missing_user_raises(make_app):
    _, session_factory = make_app()
    with session_factory() as session:
        with pytest.raises(LookupError):
            set_user_role(session, "nobody", "admin")
        with pytest.raises(ValueError):
            set_user_role(session, "viewer", "root")


# --- migration ----------------------------------------------------------------


def test_migration_makes_existing_users_admin_and_new_rows_viewer(tmp_path):
    url = f"sqlite:///{tmp_path / 'cm.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "0006")
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash) VALUES ('old', 'x'), ('older', 'y')"
            )
        )
    upgrade_database(engine)
    with engine.begin() as conn:
        roles = dict(conn.execute(text("SELECT username, role FROM users")).all())
        assert roles == {"old": "admin", "older": "admin"}
        # A row inserted from outside the library gets the least privilege.
        conn.execute(
            text("INSERT INTO users (username, password_hash) VALUES ('raw', 'z')")
        )
        assert (
            conn.execute(text("SELECT role FROM users WHERE username = 'raw'")).scalar()
            == "viewer"
        )
    engine.dispose()


def test_migration_downgrade_drops_the_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'cm.db'}"
    engine = create_engine(url)
    upgrade_database(engine)
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "0006")
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
    assert "role" not in columns
    engine.dispose()


# --- CLI ----------------------------------------------------------------------


def _run_cli(monkeypatch, argv, stdin=""):
    monkeypatch.setattr("sys.argv", ["celery-manager", *argv])
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code


def _roles(url):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return dict(conn.execute(text("SELECT username, role FROM users")).all())
    finally:
        engine.dispose()


def test_cli_create_user_defaults_to_viewer(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path / 'cm.db'}"
    assert (
        _run_cli(
            monkeypatch,
            ["create-user", "ann", "--db-url", url, "--password-stdin"],
            "secret\n",
        )
        == 0
    )
    assert "'viewer'" in capsys.readouterr().out
    assert _roles(url) == {"ann": "viewer"}


def test_cli_create_user_with_role_and_set_role(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path / 'cm.db'}"
    argv = [
        "create-user",
        "bob",
        "--db-url",
        url,
        "--role",
        "admin",
        "--password-stdin",
    ]
    assert _run_cli(monkeypatch, argv, "secret\n") == 0
    assert _roles(url) == {"bob": "admin"}

    # The password really is the one from stdin.
    engine = create_engine(url)
    from sqlalchemy.orm import sessionmaker

    from celery_manager.auth.users import verify_password

    with sessionmaker(engine)() as session:
        assert verify_password(session, "bob", "secret") is not None
    engine.dispose()

    assert _run_cli(monkeypatch, ["set-role", "bob", "operator", "--db-url", url]) == 0
    assert _roles(url) == {"bob": "operator"}

    assert _run_cli(monkeypatch, ["list-users", "--db-url", url]) == 0
    assert "bob\toperator" in capsys.readouterr().out


def test_cli_refuses_bad_input(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path / 'cm.db'}"
    assert _run_cli(monkeypatch, ["set-role", "ghost", "admin", "--db-url", url]) == 1
    assert "no dashboard user" in capsys.readouterr().err
    # argparse rejects an unknown role before touching the database.
    assert (
        _run_cli(monkeypatch, ["create-user", "c", "--db-url", url, "--role", "root"])
        == 2
    )
    assert (
        _run_cli(
            monkeypatch, ["create-user", "c", "--db-url", url, "--password-stdin"], "\n"
        )
        == 1
    )
    assert "must not be empty" in capsys.readouterr().err
    assert (
        _run_cli(
            monkeypatch,
            ["create-user", "c", "--db-url", url, "--password-stdin"],
            "pw\n",
        )
        == 0
    )
    assert (
        _run_cli(
            monkeypatch,
            ["create-user", "c", "--db-url", url, "--password-stdin"],
            "pw\n",
        )
        == 1
    )
    assert "already exists" in capsys.readouterr().err


def test_cli_prompted_passwords_must_match(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path / 'cm.db'}"
    answers = iter(["one", "two"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert _run_cli(monkeypatch, ["create-user", "d", "--db-url", url]) == 1
    assert "do not match" in capsys.readouterr().err
    assert _roles(url) == {}


def test_get_user_role(make_app):
    _, session_factory = make_app()
    with session_factory() as session:
        assert get_user_role(session, "operator") == "operator"
        assert get_user_role(session, "nobody") is None
        assert session.query(User).count() == 3
