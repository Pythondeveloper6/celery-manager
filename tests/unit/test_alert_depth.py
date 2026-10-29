from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from celery import Celery

from celery_manager import CeleryManagerConfig
from celery_manager.alerts.depth import check
from celery_manager.alerts.evaluator import CheckUnavailable
from celery_manager.control.alerts import RULE_PARAMS
from celery_manager.control.deadletter import list_dead_letters
from celery_manager.control.failures import resolution_history, resolve_failure
from celery_manager.db.models import AppSetting, TaskRun, WorkerState
from celery_manager.db.session import get_session_factory, init_db

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


@pytest.fixture
def env():
    config = CeleryManagerConfig(celery_app=Celery("depth"), db_url="sqlite:///:memory:", secret_key="s")
    engine = init_db(config)
    sessions = get_session_factory(engine)
    yield config, sessions
    engine.dispose()


def params(kind):
    return {k: spec[1] for k, spec in RULE_PARAMS[kind].items()}


def row(task_id, status="failed", seconds=100, ago=1):
    return TaskRun(task_id=task_id, task_name="demo.task", status=status, started_at=NOW-timedelta(hours=2),
                   finished_at=NOW-timedelta(minutes=ago) if status != "started" else None,
                   runtime_seconds=seconds if status != "started" else None)


@pytest.mark.parametrize("kind", ["task_failed", "consecutive_failures", "run_stuck", "worker_lost", "beat_stale", "task_sla", "duration_regression", "daily_digest"])
def test_no_evidence_is_unknown(kind, env):
    config, sessions = env
    with sessions() as session:
        probe = SimpleNamespace(session=session, now=NOW, config=config)
        with pytest.raises(CheckUnavailable):
            check(SimpleNamespace(kind=kind, target="*"), params(kind), probe)


@pytest.mark.parametrize("kind", ["task_failed", "consecutive_failures", "run_stuck", "worker_lost", "beat_stale", "task_sla", "duration_regression"])
def test_firing_recovery_and_missing_evidence(kind, env):
    config, sessions = env
    with sessions() as session:
        p = params(kind)
        if kind == "beat_stale":
            import json
            stamp = AppSetting(key="beat_last_tick", value=json.dumps({"at": (NOW-timedelta(hours=1)).isoformat()}), updated_at=NOW)
            session.add(stamp)
        elif kind == "duration_regression":
            for i in range(10):
                session.add(row(f"baseline-{i}", "succeeded", 10, 1440))
                session.add(row(f"recent-{i}", "succeeded", 100, 1))
        elif kind in ("run_stuck", "worker_lost"):
            run = row("active", "started"); run.worker = "worker"
            session.add(run)
            session.add(WorkerState(hostname="worker", state="offline", last_seen_at=NOW))
        else:
            for i in range(3): session.add(row(f"bad-{i}", seconds=10000))
        session.commit()
        probe = SimpleNamespace(session=session, now=NOW, config=config)
        rule = SimpleNamespace(kind=kind, target="*")
        assert check(rule, p, probe).firing
        if kind == "beat_stale":
            import json
            stamp.value = json.dumps({"at": NOW.isoformat()})
        elif kind == "worker_lost":
            worker = session.get(WorkerState, "worker"); worker.state="online"
        elif kind == "duration_regression":
            for r in session.query(TaskRun).filter(TaskRun.task_id.like("recent-%")): r.runtime_seconds=10
        elif kind == "consecutive_failures":
            session.add(row("new-success", "succeeded", ago=0))
        elif kind == "run_stuck":
            session.get(TaskRun, session.query(TaskRun.id).filter_by(task_id="active").scalar()).status="succeeded"
        elif kind == "task_sla":
            for r in session.query(TaskRun): r.runtime_seconds=1
        else:
            for r in session.query(TaskRun): r.status="succeeded"
        session.commit()
        assert not check(rule, p, probe).firing
        if kind == "beat_stale":
            session.delete(stamp)
        else:
            session.query(TaskRun).delete()
        session.commit()
        with pytest.raises(CheckUnavailable): check(rule,p,probe)


def test_consecutive_insufficient_samples_and_lost_worker_unknown(env):
    config,sessions=env
    with sessions() as session:
        session.add(row("one")); session.add(row("active","started")); session.commit()
        probe=SimpleNamespace(session=session,now=NOW,config=config)
        observation=check(SimpleNamespace(kind="consecutive_failures",target="*"),params("consecutive_failures"),probe)
        assert observation.unknown == {"demo.task"}
        observation=check(SimpleNamespace(kind="worker_lost",target="*"),params("worker_lost"),probe)
        assert observation.unknown == {"active"}


def test_digest_has_durable_day_key_and_disjoint_runtime_baselines(env):
    config,sessions=env
    with sessions() as session:
        session.add(row("yesterday", "succeeded", ago=1440)); session.commit()
        probe=SimpleNamespace(session=session,now=NOW,config=config)
        rule=SimpleNamespace(kind="daily_digest",target="*")
        first=check(rule,params("daily_digest"),probe)
        assert first.firing == check(rule,params("daily_digest"),probe).firing
        probe.now += timedelta(days=1)
        assert set(first.firing).isdisjoint(check(rule,params("daily_digest"),probe).firing)


def test_failure_resolution_history_and_inbox(env):
    _,sessions=env
    with sessions() as session:
        session.add(row("failure"));session.commit()
        assert list_dead_letters(session)[1] == 1
        resolve_failure(session,"failure","acknowledged","operator","Investigating")
        assert list_dead_letters(session)[1] == 0
        resolve_failure(session,"failure","acknowledged","operator","Investigating")
        assert len(resolution_history(session,"failure")) == 1
        resolve_failure(session,"failure","open","operator","Needs retry")
        assert list_dead_letters(session)[1] == 1
        resolve_failure(session,"failure","ignored","operator","Expected failure")
        assert [h.state for h in resolution_history(session,"failure")] == ["acknowledged","open","ignored"]


def test_daily_digest_delivery_is_persistently_deduplicated(env):
    from celery_manager.alerts.evaluator import evaluate_once
    from celery_manager.control.alerts import create_channel, create_rule
    config,sessions=env
    with sessions() as session:
        session.add(row("yesterday", "succeeded", ago=1440));session.commit()
        channel=create_channel(session,"admin","digest","webhook",{"url":"https://example.test/hooks"})
        create_rule(session,"admin",{"name":"daily","kind":"daily_digest","target":"*","channel_ids":[channel["id"]],"notify_resolved":False})
    messages=[]
    senders={"webhook":lambda channel,message,config:messages.append(message)}
    assert len(evaluate_once(sessions,config.celery_app,config,NOW,senders=senders)) == 1
    assert evaluate_once(sessions,config.celery_app,config,NOW+timedelta(minutes=1),senders=senders) == []
    assert messages[0].details["tasks"]["demo.task"]["succeeded"] == 1
    assert len(evaluate_once(sessions,config.celery_app,config,NOW+timedelta(days=1),senders=senders)) == 1


def test_failure_resolution_routes_roles_notes_and_history(env):
    from fastapi.testclient import TestClient

    from celery_manager.api.app import create_app
    from celery_manager.auth.external import ExternalUser
    config,_=env
    config.authenticate_request=lambda request:ExternalUser("host",request.headers.get("x-role","operator"),"person-2")
    app,sessions=create_app(config)
    with sessions() as session:
        session.add(row("api-failure")); session.commit()
    client=TestClient(app,base_url="https://testserver")
    assert client.post("/api/dead-letters/api-failure/acknowledge",headers={"x-role":"viewer"}).status_code == 403
    assert client.post("/api/dead-letters/api-failure/acknowledge",json={"note":"Investigating"}).status_code == 200
    assert client.get("/api/dead-letters").json()["total"] == 0
    history=client.get("/api/dead-letters/api-failure/history",headers={"x-role":"viewer"}).json()["items"]
    assert history[0]["actor_subject"] == "person-2" and history[0]["note"] == "Investigating"
    assert client.post("/api/dead-letters/api-failure/reopen").status_code == 200
    assert client.get("/api/dead-letters").json()["total"] == 1
    assert client.post("/api/dead-letters/missing/ignore").status_code == 404
    assert client.post("/api/dead-letters/api-failure/ignore",json={"note":"x"*2001}).status_code == 422


@pytest.mark.parametrize("unrelated", ["target", "window"])
def test_large_unrelated_history_does_not_disable_scoped_alert(env, unrelated):
    _, sessions = env
    with sessions() as session:
        session.execute(TaskRun.__table__.insert(), [
            {"task_id": f"history-{i}", "task_name": "elsewhere" if unrelated == "target" else "demo.task",
             "status": "succeeded", "finished_at": NOW-timedelta(days=100) if unrelated == "window" else NOW}
            for i in range(20001)
        ])
        session.add(row("current-failure"))
        session.commit()
        rule = SimpleNamespace(kind="task_failed", target="demo.task")
        observation = check(rule, params(rule.kind), SimpleNamespace(session=session, now=NOW))
        assert set(observation.firing) == {"current-failure"}


def test_failure_review_and_audit_share_transaction(env, monkeypatch):
    _, sessions = env
    with sessions() as session:
        session.add(row("atomic-review"))
        session.commit()
    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr("celery_manager.control.failures.record", unavailable)
    with pytest.raises(RuntimeError), sessions() as session:
        resolve_failure(session, "atomic-review", "ignored", "admin", "reason")
    with sessions() as session:
        from celery_manager.db.models import FailureResolution
        assert session.get(FailureResolution, "atomic-review") is None
        assert resolution_history(session, "atomic-review") == []
