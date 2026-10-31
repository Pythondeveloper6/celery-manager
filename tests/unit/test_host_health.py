"""Durable custom measurements and external scheduler expectations."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from celery_manager.alerts.host_health import check
from celery_manager.db.health_data import ExpectedSchedulerCommand, HealthObservation
from celery_manager.db.models import Base
from celery_manager.health_probes import (
    ProbeDefinition,
    ProbeResult,
    ProbeRunner,
    collect_aprobes,
    outbox_health,
    probe_status,
)
from celery_manager.scheduler_health import (
    ExpectedCommand,
    register_scheduler,
    retire_scheduler,
    scheduler_command_observed,
    scheduler_heartbeat,
    scheduler_status,
)

NOW = datetime(2026, 10, 29, 9, tzinfo=timezone.utc)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def config(*definitions):
    return SimpleNamespace(health_probes=definitions, probe_timeout_seconds=0.02, health_observation_retention_days=7)


def test_sync_async_results_persist_without_exception_secrets(sessions):
    async def async_provider():
        return ProbeResult("degraded", {"depth": 12})
    def broken():
        raise RuntimeError("postgresql://secret:password@private/database")
    conf = config(ProbeDefinition("good", lambda: ProbeResult("healthy", {"count": 1})),
                  ProbeDefinition("async", async_provider), ProbeDefinition("broken", broken),
                  ProbeDefinition("bad", lambda: ProbeResult("healthy", {"secret": "password"})))
    assert asyncio.run(collect_aprobes(conf, sessions)) == 4
    # A new config/process does not need the provider to read durable data.
    with sessions() as session:
        rows = {item["name"]: item for item in probe_status(session)}
        assert rows["good"]["state"] == "healthy"
        assert rows["async"]["state"] == "degraded"
        assert rows["broken"]["state"] == "unknown" and rows["broken"]["reason"] == "provider_failed"
        assert rows["bad"]["state"] == "unknown"
        assert "password" not in json.dumps(rows)
        stale = probe_status(session, now=datetime.now(timezone.utc) + timedelta(minutes=10))
        assert all(item["state"] == "unknown" for item in stale)


def test_hung_provider_is_single_flight_and_async_timeout_is_unknown():
    finish, entered = Event(), Event()
    calls = []
    def hang():
        calls.append(1)
        entered.set()
        finish.wait(2)
        return ProbeResult("healthy")
    async def hang_async():
        await asyncio.sleep(1)
        return ProbeResult("healthy")
    runner = ProbeRunner()
    try:
        started = time.monotonic()
        result = runner.collect([ProbeDefinition("hang", hang), ProbeDefinition("async", hang_async)], 0.02)
        assert time.monotonic() - started < 0.5
        assert entered.is_set() and result["hang"][0].state == "unknown"
        assert result["async"][0].state == "unknown"
        runner.collect([ProbeDefinition("hang", hang)], 0.02)
        assert calls == [1]
    finally:
        finish.set()


@pytest.mark.parametrize("metrics", [{"value": float("nan")}, {"value": True}, {"value": 1e20},
                                     {"bad key": 3}, {str(i): i for i in range(33)}])
def test_malformed_numeric_results_are_unknown(metrics):
    result = ProbeRunner().collect([ProbeDefinition("bad", lambda: ProbeResult("healthy", metrics))], 0.02)
    assert result["bad"][0].state == "unknown" and result["bad"][1] == "invalid_result"


def test_outbox_helper_uses_only_explicit_measurements():
    measurements = {"depth": 20, "oldest_age_seconds": 30, "retry_exhausted": 0, "delivery_latency_seconds": 2}
    assert outbox_health(**measurements).state == "healthy"
    assert outbox_health(**measurements, max_depth=20).state == "degraded"
    assert outbox_health(**{**measurements, "retry_exhausted": 1}).state == "degraded"
    assert outbox_health(**{**measurements, "depth": None}).state == "unknown"
    with pytest.raises(ValueError):
        outbox_health(**{**measurements, "depth": -1})


def test_first_expected_run_can_be_overdue_without_any_completion(sessions):
    with sessions() as session:
        register_scheduler(session, "orders", [ExpectedCommand("daily", "0 9 * * *", "UTC", 30)], at=NOW - timedelta(hours=1))
        # Fresh heartbeat does not hide a command that never ran.
        scheduler_heartbeat(session, "orders", at=NOW)
        status = scheduler_status(session, now=NOW + timedelta(seconds=31))[0]
        assert status["state"] == "degraded"
        assert status["commands"][0]["first_run"] is True
        assert status["commands"][0]["due_at"] == NOW.isoformat()
        scheduler_command_observed(session, "orders", "daily", at=NOW + timedelta(seconds=32))
        assert scheduler_status(session, now=NOW + timedelta(seconds=33))[0]["state"] == "healthy"


def test_independent_instance_freshness_and_registration_does_not_reset_anchor(sessions):
    with sessions() as session:
        for name in ("orders", "billing"):
            register_scheduler(session, name, [], at=NOW)
        scheduler_heartbeat(session, "orders", at=NOW + timedelta(minutes=5))
        items = {item["instance_id"]: item for item in scheduler_status(session, now=NOW + timedelta(minutes=5))}
        assert items["orders"]["state"] == "healthy" and items["billing"]["state"] == "degraded"
        register_scheduler(session, "orders", [ExpectedCommand("hourly", "0 * * * *", "Europe/Berlin")], at=NOW)
        register_scheduler(session, "orders", [ExpectedCommand("hourly", "0 * * * *", "Europe/Berlin")], at=NOW + timedelta(hours=1))
        stored = session.get(ExpectedSchedulerCommand, ("orders", "hourly"))
        assert stored.registered_at.replace(tzinfo=timezone.utc) == NOW
        # Delayed heartbeat/observation events cannot rewind liveness.
        scheduler_heartbeat(session, "orders", at=NOW)
        scheduler_command_observed(session, "orders", "hourly", at=NOW + timedelta(minutes=3))
        scheduler_command_observed(session, "orders", "hourly", at=NOW)
        assert session.get(ExpectedSchedulerCommand, ("orders", "hourly")).observed_at.replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=3)
        assert retire_scheduler(session, "billing")
        assert [item["instance_id"] for item in scheduler_status(session, now=NOW)] == ["orders"]


def test_probe_and_scheduler_alerts_unknown_firing_recovery_and_target_scope(sessions):
    with sessions() as session:
        session.add(HealthObservation(name="outbox", state="degraded", metrics='{"depth":99}', observed_at=NOW))
        session.add(HealthObservation(name="other", state="healthy", metrics="{}", observed_at=NOW))
        session.commit()
        probe = SimpleNamespace(session=session, config=config(), now=NOW)
        rule = SimpleNamespace(kind="custom_probe", target="outbox")
        assert set(check(rule, {"stale_seconds": 120}, probe).firing) == {"outbox"}
        probe.now = NOW + timedelta(minutes=5)
        assert check(rule, {"stale_seconds": 120}, probe).unknown == {"outbox"}
        stored = session.get(HealthObservation, "outbox")
        stored.state, stored.observed_at = "healthy", probe.now
        session.commit()
        assert not check(rule, {"stale_seconds": 120}, probe).firing
        stored.metrics = '"private-secret"'
        session.commit()
        assert check(rule, {"stale_seconds": 120}, probe).unknown == {"outbox"}
        register_scheduler(session, "orders", [], at=NOW)
        scheduler_rule = SimpleNamespace(kind="scheduler_stale", target="orders")
        assert set(check(scheduler_rule, {"seconds": 120}, probe).firing) == {"orders"}
        scheduler_heartbeat(session, "orders", at=probe.now)
        assert not check(scheduler_rule, {"seconds": 120}, probe).firing


def test_async_providers_stay_on_the_host_event_loop(sessions):
    async def scenario():
        loop = asyncio.get_running_loop()
        async def provider():
            assert asyncio.get_running_loop() is loop
            return ProbeResult("healthy", {"depth": 1})
        config = SimpleNamespace(health_probes=(ProbeDefinition("host-loop", provider),), probe_timeout_seconds=1,
                                 health_observation_retention_days=7)
        assert await collect_aprobes(config, sessions) == 1
        with sessions() as session:
            assert probe_status(session)[0]["state"] == "healthy"
    asyncio.run(scenario())


def test_scheduler_status_batches_queries_and_first_registration_is_concurrent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import event

    from celery_manager import CeleryManagerConfig
    from celery_manager.runtime import setup
    config = CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///{tmp_path / 'health.db'}")
    factory = setup(config)
    def register(index):
        with factory() as session:
            register_scheduler(session, "shared", [ExpectedCommand("command", "* * * * *")])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(register, range(12)))
    with factory() as session:
        for index in range(4):
            register_scheduler(session, f"owner-{index}", [ExpectedCommand("command", "* * * * *")])
        queries = []
        def before(*args): queries.append(1)
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", before)
        try:
            assert len(scheduler_status(session)) == 5
        finally:
            event.remove(engine, "before_cursor_execute", before)
        assert len(queries) == 2


def test_sync_callback_returning_host_future_is_awaited_on_host_loop(sessions):
    async def scenario():
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        loop.call_later(0.01, future.set_result, ProbeResult("healthy", {"depth": 2}))
        conf = SimpleNamespace(health_probes=(ProbeDefinition("host-future", lambda: future),),
                               probe_timeout_seconds=1, health_observation_retention_days=7)
        assert await collect_aprobes(conf, sessions) == 1
        with sessions() as session:
            assert probe_status(session)[0]["state"] == "healthy"
    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ['{}', '{"secret":"db-password"}', '[]', 'null', '{"type":"crontab","minute":"invalid"}'])
def test_malformed_scheduler_definitions_are_sanitized_unknown(sessions, raw):
    with sessions() as session:
        register_scheduler(session, "orders", [ExpectedCommand("daily", "0 9 * * *")], at=NOW)
        scheduler_heartbeat(session, "orders", at=NOW)
        row = session.get(ExpectedSchedulerCommand, ("orders", "daily"))
        row.schedule_spec = raw
        session.commit()
        status = scheduler_status(session, now=NOW)[0]
        assert status["state"] == "unknown"
        assert status["commands"][0]["schedule"] == {}
        assert "db-password" not in json.dumps(status)


def test_events_before_new_registration_cannot_claim_liveness(sessions):
    with sessions() as session:
        register_scheduler(session, "orders", [ExpectedCommand("daily", "0 9 * * *")], at=NOW)
        scheduler_command_observed(session, "orders", "daily", at=NOW - timedelta(days=1))
        scheduler_heartbeat(session, "orders", at=NOW - timedelta(seconds=1))
        assert scheduler_status(session, now=NOW)[0]["state"] == "unknown"
        row = session.get(ExpectedSchedulerCommand, ("orders", "daily"))
        assert row.observed_at is None


@pytest.mark.parametrize("kind", ["custom_probe", "scheduler_stale"])
def test_oversized_health_scope_cannot_report_healthy_partial_snapshot(sessions, kind):
    from celery_manager.alerts.evaluator import CheckUnavailable
    from celery_manager.db.health_data import SchedulerInstance
    with sessions() as session:
        if kind == "custom_probe":
            session.execute(HealthObservation.__table__.insert(), [
                {"name": f"probe-{index:04}", "state": "healthy", "metrics": "{}", "observed_at": NOW}
                for index in range(1001)])
        else:
            session.execute(SchedulerInstance.__table__.insert(), [
                {"instance_id": f"scheduler-{index:04}", "registered_at": NOW, "heartbeat_at": NOW}
                for index in range(1001)])
        session.commit()
        probe = SimpleNamespace(session=session, config=config(), now=NOW)
        with pytest.raises(CheckUnavailable, match="exceeds 1000"):
            check(SimpleNamespace(kind=kind, target="*"), {"seconds": 120, "stale_seconds": 120}, probe)


@pytest.mark.parametrize("identifier", ["Orders", "orders ", " orders", "Örders", "اوامر", "-orders", "", "a" * 129])
def test_noncanonical_scheduler_identifiers_are_rejected_before_writes(sessions, identifier):
    from celery_manager.db.health_data import SchedulerInstance
    with sessions() as session:
        with pytest.raises(ValueError, match="lowercase ASCII"):
            register_scheduler(session, identifier, [], at=NOW)
        with pytest.raises(ValueError, match="lowercase ASCII"):
            register_scheduler(session, "orders", [ExpectedCommand(identifier, "* * * * *")], at=NOW)
        assert session.query(SchedulerInstance).count() == 0
        register_scheduler(session, "orders", [ExpectedCommand("daily", "0 9 * * *")], at=NOW)
        for action in (scheduler_heartbeat, retire_scheduler):
            with pytest.raises(ValueError, match="lowercase ASCII"):
                action(session, identifier)
        with pytest.raises(ValueError, match="lowercase ASCII"):
            scheduler_command_observed(session, "orders", identifier)
        with pytest.raises(ValueError, match="lowercase ASCII"):
            scheduler_command_observed(session, identifier, "daily")
        assert session.get(SchedulerInstance, "orders").heartbeat_at is None
        assert session.get(ExpectedSchedulerCommand, ("orders", "daily")).observed_at is None


def test_scheduler_identifiers_accept_portable_separators_and_maximum_length(sessions):
    identifier = "0prod.scheduler:region_1-" + "a" * 103
    assert len(identifier) == 128
    with sessions() as session:
        register_scheduler(session, identifier, [ExpectedCommand(identifier, "* * * * *")], at=NOW)
        scheduler_heartbeat(session, identifier, at=NOW)
        scheduler_command_observed(session, identifier, identifier, at=NOW)
        status = scheduler_status(session, now=NOW)[0]
        assert status["instance_id"] == identifier and status["state"] == "healthy"
        assert retire_scheduler(session, identifier)


@pytest.mark.parametrize("identifier", ["Orders", "orders\n", "ordérs", "a" * 129])
def test_scheduler_retirement_path_rejects_noncanonical_ids(identifier):
    from typing import get_type_hints

    from pydantic import TypeAdapter, ValidationError

    from celery_manager.api.routes.custom_health import retire
    parameter = get_type_hints(retire, include_extras=True)["instance_id"]
    with pytest.raises(ValidationError):
        TypeAdapter(parameter).validate_python(identifier)
