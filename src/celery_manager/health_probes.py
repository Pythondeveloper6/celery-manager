"""Host-neutral, sanitized health measurements collected outside HTTP requests."""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import threading
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import delete

from celery_manager.db.health_data import HealthObservation


class HealthScopeUnavailable(ValueError):
    """A partial health snapshot cannot safely establish recovery."""


@dataclass(frozen=True)
class ProbeResult:
    state: str
    metrics: dict[str, float] = field(default_factory=dict)


class ProbeProvider(Protocol):
    def __call__(self) -> ProbeResult | Awaitable[ProbeResult]: ...


@dataclass(frozen=True)
class ProbeDefinition:
    name: str
    provider: ProbeProvider


def validate_probes(definitions):
    if not isinstance(definitions, (tuple, list)) or len(definitions) > 32:
        raise ValueError("Configure at most 32 health probes")
    names = set()
    for item in definitions:
        if not isinstance(item, ProbeDefinition) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", item.name):
            raise ValueError("Probe names must be unique safe identifiers up to 64 characters")
        if item.name in names or not callable(item.provider):
            raise ValueError("Probe names must be unique and providers callable")
        names.add(item.name)


def _validated(result):
    if not isinstance(result, ProbeResult) or result.state not in ("healthy", "degraded", "unknown"):
        raise ValueError("Invalid probe result")
    if not isinstance(result.metrics, dict) or len(result.metrics) > 32:
        raise ValueError("Invalid probe metrics")
    metrics = {}
    for key, value in result.metrics.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise ValueError("Invalid metric name")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e12:
            raise ValueError("Probe metrics must be bounded finite numbers")
        metrics[key] = value
    return ProbeResult(result.state, metrics)


def outbox_health(*, depth, oldest_age_seconds, retry_exhausted, delivery_latency_seconds,
                  max_depth=None, max_oldest_age_seconds=None, max_delivery_latency_seconds=None):
    """Host supplies aggregate measurements; this helper never queries its schema."""
    metrics = {"depth": depth, "oldest_age_seconds": oldest_age_seconds,
               "retry_exhausted": retry_exhausted, "delivery_latency_seconds": delivery_latency_seconds}
    if any(value is None for value in metrics.values()):
        return ProbeResult("unknown")
    _validated(ProbeResult("healthy", metrics))
    if any(value < 0 for value in metrics.values()):
        raise ValueError("Outbox measurements cannot be negative")
    limits = [(depth, max_depth), (oldest_age_seconds, max_oldest_age_seconds),
              (delivery_latency_seconds, max_delivery_latency_seconds)]
    for _, limit in limits:
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float))
                                  or not math.isfinite(limit) or not 0 <= limit <= 1e12):
            raise ValueError("Outbox thresholds must be bounded nonnegative numbers")
    degraded = retry_exhausted > 0 or any(limit is not None and value >= limit for value, limit in limits)
    return ProbeResult("degraded" if degraded else "healthy", metrics)


class _Run:
    def __init__(self, definition, timeout, host_loop=None):
        self.result = None
        self.reason = None
        self.observed_at = None
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._work, args=(definition, timeout, host_loop), daemon=True,
                                       name="cm-health-probe")
        self.thread.start()

    def _work(self, definition, timeout, host_loop):
        async def resolve(awaitable):
            return await asyncio.wait_for(awaitable, timeout)
        try:
            value = definition.provider()
            if inspect.isawaitable(value):
                value = (asyncio.run_coroutine_threadsafe(resolve(value), host_loop).result()
                         if host_loop is not None else asyncio.run(resolve(value)))
            try:
                self.result = _validated(value)
            except (ValueError, TypeError):
                self.reason = "invalid_result"
        except asyncio.TimeoutError:
            self.reason = "provider_timeout"
        except Exception:  # noqa: BLE001 - never persist/log host exceptions or their secrets
            self.reason = "provider_failed"
        finally:
            self.observed_at = datetime.now(timezone.utc)


class ProbeRunner:
    """One daemon invocation per name; a hung synchronous provider cannot pile up."""
    def __init__(self):
        self.runs = {}
        self.lock = threading.Lock()

    def collect(self, definitions, timeout, *, host_loop=None):
        validate_probes(definitions)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0.01 <= timeout <= 30:
            raise ValueError("probe_timeout_seconds must be between 0.01 and 30")
        with self.lock:
            names = {item.name for item in definitions}
            # Keep hung runs until they finish, even when configuration changes.
            self.runs = {name: run for name, run in self.runs.items() if name in names or run.thread.is_alive()}
            if len(self.runs) > 64:
                raise ValueError("Too many pending probes; restart collector after repairing providers")
            active = {}
            for item in definitions:
                previous = self.runs.get(item.name)
                if previous is None or not previous.thread.is_alive():
                    previous = self.runs[item.name] = _Run(item, timeout, host_loop)
                active[item.name] = previous
            deadline = time.monotonic() + timeout
            result = {}
            for name, run in active.items():
                run.thread.join(max(0, deadline - time.monotonic()))
                if run.thread.is_alive():
                    result[name] = (ProbeResult("unknown"), "provider_timeout", datetime.now(timezone.utc))
                else:
                    result[name] = (run.result or ProbeResult("unknown"), run.reason, run.observed_at)
            return result


_RUNNER_GUARD = threading.Lock()


def _runner_for(config):
    with _RUNNER_GUARD:
        runner = getattr(config, "_cm_probe_runner", None)
        if runner is None:
            runner = config._cm_probe_runner = ProbeRunner()
        return runner


def collect_probes(config, sessions):
    """Explicit observer poll; HTTP endpoints read stored observations only."""
    definitions = getattr(config, "health_probes", ())
    runner = _runner_for(config)
    results = runner.collect(definitions, getattr(config, "probe_timeout_seconds", 3),
                             host_loop=getattr(config, "_cm_probe_host_loop", None))
    retention = getattr(config, "health_observation_retention_days", 7)
    if isinstance(retention, bool) or not isinstance(retention, int) or not 1 <= retention <= 3650:
        raise ValueError("health_observation_retention_days must be between 1 and 3650")
    with sessions() as session:
        for name in sorted(results):
            result, reason, stamp = results[name]
            _persist_observation(session, name, result, reason, stamp)
        # Indexed, bounded deletion; expected but absent probes remain unknown in the API.
        expired = list(session.query(HealthObservation.name).filter(
            HealthObservation.observed_at < datetime.now(timezone.utc) - timedelta(days=retention)).limit(1000))
        if expired:
            session.execute(delete(HealthObservation).where(HealthObservation.name.in_([item[0] for item in expired]), HealthObservation.observed_at < datetime.now(timezone.utc) - timedelta(days=retention)))
        session.commit()
    return len(results)



def _persist_observation(session, name, result, reason, stamp):
    from sqlalchemy import case, insert, select
    from sqlalchemy.exc import IntegrityError

    from celery_manager.control.health import aware
    values = {"name": name, "state": result.state, "metrics": json.dumps(result.metrics),
              "reason": reason, "observed_at": stamp}
    dialect = session.get_bind().dialect.name
    if dialect in ("sqlite", "postgresql"):
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        statement = dialect_insert(HealthObservation).values(**values)
        session.execute(statement.on_conflict_do_update(index_elements=["name"], set_={key: getattr(statement.excluded, key) for key in values if key != "name"}, where=HealthObservation.observed_at <= stamp))
        return
    if dialect in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import insert as dialect_insert
        statement = dialect_insert(HealthObservation).values(**values)
        newer = statement.inserted.observed_at >= HealthObservation.observed_at
        session.execute(statement.on_duplicate_key_update(**{key: case((newer, getattr(statement.inserted, key)), else_=getattr(HealthObservation, key)) for key in values if key != "name"}))
        return
    query = select(HealthObservation).where(HealthObservation.name == name).with_for_update().with_hint(HealthObservation, "WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql").execution_options(populate_existing=True)
    row = session.scalar(query)
    if row is None:
        try:
            with session.begin_nested():
                session.execute(insert(HealthObservation).values(**values))
            return
        except IntegrityError:
            row = session.scalar(query)
    if row is not None and aware(row.observed_at) <= stamp:
        for key, value in values.items():
            setattr(row, key, value)

def probe_status(session, *, freshness_seconds=120, now=None, expected_names=()):
    from celery_manager.control.health import aware
    now = aware(now or datetime.now(timezone.utc))
    rows = session.query(HealthObservation).order_by(HealthObservation.name).limit(1001).all()
    if len(rows) > 1000:
        raise HealthScopeUnavailable("Health observation scope exceeds 1000; retire obsolete registrations")
    items = {}
    for row in rows:
        age = max(0, (now - aware(row.observed_at)).total_seconds())
        try:
            value = _validated(ProbeResult(row.state, json.loads(row.metrics)))
            state, metrics, reason = value.state, value.metrics, row.reason
        except (ValueError, TypeError):
            state, metrics, reason = "unknown", {}, "invalid_observation"
        if age > freshness_seconds:
            state, reason = "unknown", "stale_observation"
        items[row.name] = {"name": row.name, "state": state, "metrics": metrics, "reason": reason,
                           "observed_at": aware(row.observed_at).isoformat(), "age_seconds": age}
    for name in expected_names:
        items.setdefault(name, {"name": name, "state": "unknown", "metrics": {}, "reason": "not_observed",
                                "observed_at": None, "age_seconds": None})
    return [items[name] for name in sorted(items)]


async def collect_aprobes(config, sessions):
    """Persist off-loop while every awaitable resolves on the owning host loop."""
    from copy import copy
    _runner_for(config)
    local = copy(config)
    local._cm_probe_host_loop = asyncio.get_running_loop()
    return await asyncio.to_thread(collect_probes, local, sessions)
