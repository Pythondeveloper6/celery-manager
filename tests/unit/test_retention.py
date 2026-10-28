"""Data retention: rules, pruning, the CLI, the optional Celery task and the API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager import cli
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.retention import (
    RetentionRules,
    get_last_prune,
    get_retention,
    prune_audit_log,
    prune_runs,
    run_prune,
    update_retention,
)
from celery_manager.db.models import AuditLog, TaskRun
from celery_manager.db.session import get_session_factory, init_db
from celery_manager.tasks import PRUNE_BEAT_ENTRY, register_prune_task

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


@pytest.fixture
def session():
    config = CeleryManagerConfig(celery_app=Celery("t"), db_url="sqlite:///:memory:")
    with get_session_factory(init_db(config))() as s:
        yield s


def add_run(session, task_id, name="demo.a", status="succeeded", age=None, **extra):
    """A run that finished `age` days ago (None: never finished)."""
    finished = days_ago(age) if age is not None else None
    session.add(
        TaskRun(
            task_id=task_id,
            task_name=name,
            status=status,
            finished_at=finished,
            updated_at=extra.pop("updated_at", finished or NOW),
            **extra,
        )
    )
    session.commit()


def remaining(session):
    return {r[0] for r in session.query(TaskRun.task_id)}


# --- defaults and rules ------------------------------------------------------


def test_defaults_delete_nothing(session):
    add_run(session, "old", age=5000)
    assert get_retention(session) == RetentionRules()
    assert prune_runs(session, now=NOW) == {}
    assert run_prune(session, now=NOW)["runs_deleted"] == 0
    assert remaining(session) == {"old"}


def test_default_days_deletes_only_older_runs(session):
    add_run(session, "old", age=40)
    add_run(session, "fresh", age=5)
    update_retention(session, {"default_days": 30}, "admin")
    assert prune_runs(session, now=NOW) == {"demo.a": 1}
    assert remaining(session) == {"fresh"}


def test_per_task_override_and_keep_forever(session):
    add_run(session, "a-old", "demo.a", age=10)
    add_run(session, "b-old", "demo.b", age=10)
    add_run(session, "c-old", "demo.c", age=10)
    update_retention(session, {"default_days": 30, "tasks": {"demo.a": 7, "demo.c": None}}, "admin")
    assert prune_runs(session, now=NOW) == {"demo.a": 1}
    assert remaining(session) == {"b-old", "c-old"}


def test_override_without_a_default_only_touches_that_task(session):
    add_run(session, "a-old", "demo.a", age=10)
    add_run(session, "b-old", "demo.b", age=10)
    update_retention(session, {"tasks": {"demo.a": 7}}, "admin")
    assert prune_runs(session, now=NOW) == {"demo.a": 1}
    assert remaining(session) == {"b-old"}


def test_failed_runs_are_kept_longer(session):
    add_run(session, "ok", age=10)
    add_run(session, "bad-mid", status="failed", age=10)
    add_run(session, "bad-old", status="failed", age=50)
    update_retention(session, {"default_days": 7, "failed_days": 30}, "admin")
    prune_runs(session, now=NOW)
    assert remaining(session) == {"bad-mid"}


def test_a_retry_outlives_the_pruned_window_while_its_failed_original_is_kept(session):
    """Otherwise the original would become a dead letter again."""
    add_run(session, "orig", status="failed", age=10)
    add_run(session, "retry", age=9, retried_from_id="orig")
    update_retention(session, {"default_days": 7, "failed_days": 30}, "admin")
    prune_runs(session, now=NOW)
    assert remaining(session) == {"orig", "retry"}


def test_validation_rejects_bad_values(session):
    for bad in ({"default_days": 0}, {"default_days": "7"}, {"default_days": True}, {"default_days": 10**6},
                {"tasks": {"x": -1}}, {"tasks": []}, {"bogus": 1}):
        with pytest.raises(ValueError):
            update_retention(session, bad, "admin")
    with pytest.raises(ValueError, match="at least default_days"):
        update_retention(session, {"default_days": 30, "failed_days": 7}, "admin")
    assert get_retention(session) == RetentionRules()


def test_damaged_stored_rules_read_as_keep_everything(session):
    from celery_manager.db.models import AppSetting

    session.add(AppSetting(key="retention", value="{not json"))
    session.commit()
    assert get_retention(session) == RetentionRules()


# --- what is never deleted ---------------------------------------------------


def test_active_runs_younger_than_the_window_are_untouched(session):
    add_run(session, "queued-new", status="queued", updated_at=days_ago(1))
    add_run(session, "started-new", status="started", updated_at=days_ago(2))
    add_run(session, "started-stale", status="started", updated_at=days_ago(40))
    add_run(session, "queued-future", status="queued", updated_at=days_ago(40), eta=NOW + timedelta(days=2))
    update_retention(session, {"default_days": 30}, "admin")
    prune_runs(session, now=NOW)
    assert remaining(session) == {"queued-new", "started-new", "queued-future"}


def test_never_finished_row_uses_updated_at(session):
    add_run(session, "lost", status="retried", updated_at=days_ago(60))
    add_run(session, "recent-lost", status="retried", updated_at=days_ago(3))
    update_retention(session, {"default_days": 30}, "admin")
    prune_runs(session, now=NOW)
    assert remaining(session) == {"recent-lost"}


def test_pruning_leaves_lineage_readable(session):
    from celery_manager.control.tasks import list_retries_of
    from celery_manager.control.workflows import get_workflow

    add_run(session, "parent", age=60)
    add_run(session, "child", age=1, retried_from_id="parent", parent_id="parent", root_id="parent")
    update_retention(session, {"default_days": 30}, "admin")
    prune_runs(session, now=NOW)
    assert [r.task_id for r in list_retries_of(session, "parent")] == ["child"]
    assert get_workflow(session, "child")["current"] == "child"


# --- batching, dry run -------------------------------------------------------


def test_deletes_in_batches(session, monkeypatch):
    for i in range(25):
        add_run(session, f"r{i}", age=100)
    update_retention(session, {"default_days": 30}, "admin")
    commits = []
    real_commit = session.commit
    monkeypatch.setattr(session, "commit", lambda: (commits.append(1), real_commit())[1])
    assert prune_runs(session, now=NOW, batch_size=10) == {"demo.a": 25}
    assert len(commits) == 3  # 10 + 10 + 5
    assert remaining(session) == set()


def test_batch_size_is_validated(session):
    with pytest.raises(ValueError):
        prune_runs(session, now=NOW, batch_size=0)


def test_dry_run_counts_without_deleting_or_recording(session):
    add_run(session, "old", age=100)
    update_retention(session, {"default_days": 30}, "admin")
    session.query(AuditLog).delete()
    session.commit()
    summary = run_prune(session, now=NOW, dry_run=True)
    assert summary["runs_deleted"] == 1 and summary["dry_run"] is True
    assert remaining(session) == {"old"}
    assert get_last_prune(session) is None
    assert session.query(AuditLog).count() == 0


# --- audit -------------------------------------------------------------------


def test_prune_writes_an_audit_row_and_a_last_prune_record(session):
    add_run(session, "old", age=100)
    update_retention(session, {"default_days": 30}, "admin")
    run_prune(session, username="cli", now=NOW)
    row = session.query(AuditLog).filter_by(action="prune").one()
    assert row.username == "cli" and row.target_type == "retention"
    assert json.loads(row.details)["by_task"] == {"demo.a": 1}
    last = get_last_prune(session)
    assert last["runs_deleted"] == 1 and last["by"] == "cli"


def test_a_prune_that_deletes_nothing_does_not_spam_the_audit_log(session):
    update_retention(session, {"default_days": 30}, "admin")
    run_prune(session, now=NOW)
    assert session.query(AuditLog).filter_by(action="prune").count() == 0
    assert get_last_prune(session)["runs_deleted"] == 0


def test_changing_retention_is_audited(session):
    update_retention(session, {"default_days": 30}, "alice")
    update_retention(session, {"default_days": 30}, "alice")  # unchanged: no second row
    rows = session.query(AuditLog).filter_by(target_type="retention", action="update").all()
    assert len(rows) == 1 and rows[0].username == "alice"


def test_audit_log_is_kept_unless_audit_days_is_set(session):
    session.add(AuditLog(username="u", action="x", target_type="t", target_id="1", created_at=days_ago(400)))
    session.commit()
    assert prune_audit_log(session, now=NOW) == 0
    update_retention(session, {"audit_days": 90}, "admin")
    assert prune_audit_log(session, now=NOW, dry_run=True) == 1
    assert prune_audit_log(session, now=NOW) == 1
    assert session.query(AuditLog).filter_by(action="x").count() == 0


# --- CLI and Celery task -----------------------------------------------------


def _seed_file_db(tmp_path):
    url = f"sqlite:///{tmp_path / 'cm.db'}"
    config = CeleryManagerConfig(celery_app=Celery("t"), db_url=url)
    engine = init_db(config)
    with get_session_factory(engine)() as s:
        add_run(s, "old", age=100)
        add_run(s, "new", age=1)
        update_retention(s, {"default_days": 30}, "admin")
    engine.dispose()
    return url


def _ids(url):
    engine = init_db(CeleryManagerConfig(celery_app=Celery("t"), db_url=url))
    with get_session_factory(engine)() as s:
        ids = remaining(s)
    engine.dispose()
    return ids


def test_cli_prune_dry_run_then_real(tmp_path, monkeypatch, capsys):
    url = _seed_file_db(tmp_path)
    monkeypatch.setattr("sys.argv", ["celery-manager", "prune", "--db-url", url, "--dry-run"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert "Would delete 1 run" in capsys.readouterr().out
    assert _ids(url) == {"old", "new"}

    monkeypatch.setattr("sys.argv", ["celery-manager", "prune", "--db-url", url])
    with pytest.raises(SystemExit):
        cli.main()
    assert "Deleted 1 run" in capsys.readouterr().out
    assert _ids(url) == {"new"}


def test_cli_prune_with_no_rules_deletes_nothing(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'cm.db'}"
    assert cli.prune_command(url) == 0
    assert "Nothing to prune" in capsys.readouterr().out


def test_prune_task_is_opt_in_and_runs_the_prune(tmp_path):
    url = _seed_file_db(tmp_path)
    app = Celery("t")
    assert "celery_manager.tasks.prune" not in app.tasks
    config = CeleryManagerConfig(celery_app=app, db_url=url)
    task = register_prune_task(app, config, schedule=3600.0)
    assert app.conf.beat_schedule[PRUNE_BEAT_ENTRY]["task"] == "celery_manager.tasks.prune"
    result = task.apply().get()
    assert result["runs_deleted"] == 1
    assert _ids(url) == {"new"}


# --- API ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def api():
    config = CeleryManagerConfig(celery_app=Celery("t"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as s:
        for role in ("viewer", "admin"):
            create_user(s, role, "pw", role=role)
    clients = {}
    for role in ("viewer", "admin"):
        c = TestClient(app, base_url="https://testserver")
        assert c.post("/api/auth/login", json={"username": role, "password": "pw"}).status_code == 200
        clients[role] = c
    return clients, session_factory


def test_api_get_is_open_to_any_signed_in_account_and_not_to_anonymous(api):
    clients, _ = api
    body = clients["viewer"].get("/api/settings/retention").json()
    assert body["default_days"] is None and body["tasks"] == {} and body["last_prune"] is None
    assert isinstance(body["known_tasks"], list)
    anonymous = TestClient(clients["viewer"].app, base_url="https://testserver")
    assert anonymous.get("/api/settings/retention").status_code == 401
    # Retention is not part of the anonymous branding payload.
    assert "default_days" not in anonymous.get("/api/settings").json()


def test_api_put_is_admin_only_and_validates(api):
    clients, _ = api
    assert clients["viewer"].put("/api/settings/retention", json={"default_days": 5}).status_code == 403
    bad = clients["admin"].put("/api/settings/retention", json={"default_days": 0})
    assert bad.status_code == 422 and "default_days" in bad.json()["detail"]
    assert clients["admin"].put("/api/settings/retention", json={"nope": 1}).status_code == 422
    ok = clients["admin"].put("/api/settings/retention", json={"default_days": 14, "tasks": {"demo.a": 3}})
    assert ok.status_code == 200 and ok.json()["default_days"] == 14 and ok.json()["tasks"] == {"demo.a": 3}
    assert clients["viewer"].get("/api/settings/retention").json()["default_days"] == 14
    clients["admin"].put("/api/settings/retention", json={"default_days": None, "tasks": {}})


def test_api_preview_counts_a_candidate_without_deleting(api):
    clients, session_factory = api
    with session_factory() as s:
        add_run(s, "api-old", "demo.preview", age=3650)
        add_run(s, "api-new", "demo.preview", age=0)
    # Nothing stored: nothing would be deleted.
    assert clients["viewer"].post("/api/settings/retention/preview", json={}).json()["runs"] == 0
    candidate = clients["viewer"].post("/api/settings/retention/preview", json={"default_days": 30})
    assert candidate.json()["by_task"].get("demo.preview") == 1
    assert candidate.json()["runs"] >= 1
    bad = clients["viewer"].post("/api/settings/retention/preview", json={"default_days": -2})
    assert bad.status_code == 422
    with session_factory() as s:
        assert {"api-old", "api-new"} <= remaining(s)
