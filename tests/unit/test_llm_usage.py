"""LLM usage recording: from a response object or by hand, priced from config,
summarised for the dashboard, alertable, and never able to break a task."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager import CeleryManagerConfig, record_llm_usage
from celery_manager import decorator as decorator_module
from celery_manager import llm as llm_module
from celery_manager.alerts.depth import check
from celery_manager.control.alerts import RULE_PARAMS
from celery_manager.control.analytics import UnknownRange
from celery_manager.control.llm import prune_llm_calls, run_usage, usage_summary
from celery_manager.db.llm import LlmCall
from celery_manager.db.models import Base
from celery_manager.decorator import track
from celery_manager.llm import (
    compute_cost,
    llm_call,
    usage_from_response,
    validate_prices,
)

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
PRICES = {"claude-test": {"input": 3.0, "output": 15.0, "cache_read": 0.3}, "claude-test-big": {"input": 15.0, "output": 75.0}}


@pytest.fixture
def env(monkeypatch):
    app = Celery("llm_test", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s", llm_prices=PRICES)
    # Register the globals with monkeypatch so they are restored, then wire
    # the app the way setup(config) does.
    for name, value in (("_config", None), ("_session_factory", None), ("_eta_tolerance_seconds", 60)):
        monkeypatch.setattr(decorator_module, name, value)
    decorator_module.configure(Session, config=config)
    monkeypatch.setattr(llm_module, "_warned", False)
    return app, Session


def anthropic_response():
    return SimpleNamespace(
        model="claude-test",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=500, cache_creation_input_tokens=0),
        content=[SimpleNamespace(type="text"), SimpleNamespace(type="tool_use"), SimpleNamespace(type="tool_use")],
    )


def test_usage_from_anthropic_and_openai_shapes():
    found = usage_from_response(anthropic_response())
    assert found == {"model": "claude-test", "provider": "anthropic", "input_tokens": 1000, "output_tokens": 200,
                     "cache_read_tokens": 500, "cache_write_tokens": 0, "tool_calls": 2}
    openai = {"model": "gpt-x", "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 40}},
              "choices": [{"message": {"tool_calls": [{}]}}]}
    found = usage_from_response(openai)
    assert (found["provider"], found["input_tokens"], found["cache_read_tokens"], found["tool_calls"]) == ("openai", 60, 40, 1)
    assert usage_from_response(object()) == {}
    assert usage_from_response({"usage": {"input_tokens": -5, "output_tokens": "x"}})["input_tokens"] == 0


def test_cost_uses_longest_prefix_and_cache_price():
    assert compute_cost(PRICES, "claude-test-2026", 1_000_000, 0) == 3.0
    assert compute_cost(PRICES, "claude-test-big-1", 1_000_000, 0) == 15.0
    assert compute_cost(PRICES, "claude-test", 0, 0, cache_read_tokens=1_000_000) == 0.3
    assert compute_cost(PRICES, "other", 10, 10) is None
    assert compute_cost({}, "claude-test", 10, 10) is None


def test_price_validation():
    validate_prices(PRICES)
    for bad in ({"m": {"input": 1}}, {"m": {"input": -1, "output": 1}}, {"m": {"input": 1, "output": 1, "x": 1}}, {"": {"input": 1, "output": 1}}, []):
        with pytest.raises(ValueError):
            validate_prices(bad)
    with pytest.raises(ValueError):
        CeleryManagerConfig(celery_app=Celery("v"), db_url="sqlite:///:memory:", secret_key="s", llm_prices={"m": {"input": 1}})
    with pytest.raises(ValueError):
        CeleryManagerConfig(celery_app=Celery("v"), db_url="sqlite:///:memory:", secret_key="s", llm_call_retention_days=-1)


def test_usage_is_attached_to_the_running_task(env):
    app, Session = env

    @app.task(name="llm.summarize", bind=True)
    @track()
    def summarize(self):
        record_llm_usage(response=anthropic_response())
        with llm_call("claude-test") as call:
            call.set(input_tokens=10, output_tokens=5)
        return "ok"

    assert summarize.apply(task_id="t-1").state == "SUCCESS"
    with Session() as session:
        rows = session.query(LlmCall).order_by(LlmCall.id).all()
        assert [r.task_id for r in rows] == ["t-1", "t-1"]
        assert rows[0].task_name == "llm.summarize"
        assert (rows[0].input_tokens, rows[0].cache_read_tokens, rows[0].tool_calls) == (1000, 500, 2)
        assert rows[0].cost_usd == pytest.approx((1000 * 3 + 200 * 15 + 500 * 0.3) / 1e6)
        assert rows[1].duration_ms is not None and rows[1].status == "ok"
        totals = run_usage(session, "t-1")["totals"]
        assert totals["calls"] == 2 and totals["input_tokens"] == 1010


def test_explicit_values_beat_the_response_and_bad_cost_is_ignored(env):
    _, Session = env
    record_llm_usage("claude-test", 7, 3, cost_usd=1.5, response=anthropic_response())
    record_llm_usage("claude-test", 1, 1, cost_usd=float("nan"))
    with Session() as session:
        first, second = session.query(LlmCall).order_by(LlmCall.id).all()
        assert (first.input_tokens, first.output_tokens, first.cost_usd, first.task_id) == (7, 3, 1.5, None)
        assert second.cost_usd == pytest.approx(18 / 1e6)


def test_llm_call_records_failures_and_reraises(env):
    _, Session = env
    with pytest.raises(RuntimeError), llm_call("claude-test"):
        raise RuntimeError("provider down")
    with Session() as session:
        row = session.query(LlmCall).one()
        assert row.status == "error" and row.duration_ms is not None


def test_recording_never_raises_and_warns_once_when_unconfigured(monkeypatch, caplog):
    monkeypatch.setattr(decorator_module, "_config", None)
    monkeypatch.setattr(decorator_module, "_session_factory", None)
    monkeypatch.setattr(llm_module, "_warned", False)
    record_llm_usage("claude-test", 1, 1)
    record_llm_usage("claude-test", 1, 1)
    record_llm_usage()  # no model
    assert sum("never called celery_manager.setup" in r.message for r in caplog.records) == 1


def test_a_broken_database_does_not_break_the_task(env, monkeypatch):
    app, _ = env

    def boom():
        raise RuntimeError("db down")

    @app.task(name="llm.broken", bind=True)
    @track()
    def work(self):
        record_llm_usage("claude-test", 1, 1)
        return "fine"

    monkeypatch.setattr(decorator_module, "_session_factory", lambda: boom())
    assert work.apply(task_id="t-x").result == "fine"


def seed(Session):
    with Session() as session:
        for i, (task, model, cost, ago, status) in enumerate([
            ("a.task", "claude-test", 1.0, 5, "ok"), ("a.task", "claude-test", 2.0, 10, "error"),
            ("b.task", "claude-test-big", 4.0, 30, "ok"), ("b.task", "claude-test", None, 90, "ok"),
        ]):
            session.add(LlmCall(task_id=f"id{i}", task_name=task, model=model, input_tokens=100, output_tokens=10,
                                cost_usd=cost, duration_ms=50.0, status=status, created_at=NOW - timedelta(minutes=ago)))
        session.commit()


def test_usage_summary(env):
    _, Session = env
    seed(Session)
    with Session() as session:
        summary = usage_summary(session, "24h", now=NOW)
        assert summary["totals"]["calls"] == 4 and summary["totals"]["errors"] == 1
        assert summary["totals"]["cost_usd"] == 7.0 and summary["totals"]["unpriced_calls"] == 1
        assert summary["by_model"][0]["model"] == "claude-test"
        assert summary["by_task"][0]["task_name"] == "b.task" and summary["by_task"][0]["cost_usd"] == 4.0
        assert sum(b["calls"] for b in summary["series"]) == 4
        assert usage_summary(session, "1h", "a.task", now=NOW)["totals"]["calls"] == 2
        assert usage_summary(session, "1h", now=NOW)["totals"]["calls"] == 3
        with pytest.raises(UnknownRange):
            usage_summary(session, "bogus")


def test_llm_cost_alert(env):
    config, Session = env
    seed(Session)
    params = {k: spec[1] for k, spec in RULE_PARAMS["llm_cost"].items()}
    params["usd"] = 3.0
    with Session() as session:
        probe = SimpleNamespace(session=session, now=NOW, config=config)
        firing = check(SimpleNamespace(kind="llm_cost", target="*"), params, probe).firing
        assert set(firing) == {"a.task", "b.task"} and firing["a.task"]["cost_usd"] == 3.0
        assert set(check(SimpleNamespace(kind="llm_cost", target="b.*"), params, probe).firing) == {"b.task"}
        params["usd"] = 100.0
        assert check(SimpleNamespace(kind="llm_cost", target="*"), params, probe).firing == {}


def test_retention(env):
    _, Session = env
    seed(Session)
    with Session() as session:
        assert prune_llm_calls(session, 0, now=NOW) == 0
        assert prune_llm_calls(session, 1, now=NOW + timedelta(days=1, hours=1), dry_run=True) == 4
        assert session.query(LlmCall).count() == 4
        assert prune_llm_calls(session, 1, now=NOW + timedelta(days=1, hours=1)) == 4
        assert session.query(LlmCall).count() == 0


def test_llm_api_requires_login_and_returns_usage():
    from fastapi.testclient import TestClient

    from celery_manager.api.app import create_app
    from celery_manager.auth.users import create_user

    config = CeleryManagerConfig(celery_app=Celery("api"), db_url="sqlite:///:memory:", secret_key="s")
    app, factory = create_app(config)
    with factory() as session:
        create_user(session, "alice", "pw-long-enough")
        session.add(LlmCall(task_id="r1", task_name="a.task", model="m", input_tokens=5, output_tokens=2, cost_usd=0.5,
                            created_at=datetime.now(timezone.utc)))
        session.commit()
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/llm/usage").status_code == 401
    client.post("/api/auth/login", json={"username": "alice", "password": "pw-long-enough"})
    body = client.get("/api/llm/usage?range=24h").json()
    assert body["totals"]["calls"] == 1 and body["by_model"][0]["model"] == "m"
    assert client.get("/api/v1/llm/usage?range=24h&task_name=a.task").json()["totals"]["cost_usd"] == 0.5
    assert client.get("/api/llm/usage?range=bogus").status_code == 422
    assert client.get("/api/llm/runs/r1").json()["totals"]["calls"] == 1
    assert client.get("/api/llm/runs/none").json()["calls"] == []


def test_llms_full_txt_is_current():
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "build_llms_txt.py"
    result = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout
