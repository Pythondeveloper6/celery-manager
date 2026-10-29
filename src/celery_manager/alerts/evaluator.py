"""The alerts evaluator: judge every enabled rule, open and resolve
incidents, send messages.

`evaluate_once` is one pass; `run_forever` repeats it. Several evaluators
may run against one database (two `celery-manager alerts` processes for
redundancy, or one per events listener with `--with-alerts`): every
decision that leads to a message is a database write that only one process
can win, so each message is sent once.

- **Opening** an incident inserts a row whose unique `open_key` is
  `"<rule id>:<key>"`. The loser of a race gets an IntegrityError and picks
  up the winner's row.
- **Notifying** claims the incident with `UPDATE ... SET notified_at = now
  WHERE notified_at IS NULL`; only the process whose UPDATE changed a row
  sends. **Resolving** is the same compare-and-set on `state`.
- **Quiet period**: a new incident for a rule and key whose previous
  incident was announced less than `quiet_period_minutes` ago is recorded
  but not announced. If it is still firing when the quiet period runs out,
  it is announced then; if it clears first, nobody hears about it, not
  even that it resolved. While an incident stays open there are no
  reminders: one incident, one message (plus "resolved", if the rule asks).
- **Unknown is not fine.** A rule that can't be judged (broker unreachable,
  a queue whose depth the transport can't report) neither opens nor
  resolves anything; its `last_check_error` says why.

Delivery failures never stop the pass: each channel is tried on its own,
its outcome recorded on the channel row. If *every* channel of a firing
message fails, the claim is released so the next pass tries again; a
partial failure is not retried (it would repeat the message to the
channels that got it) and is recorded on the incident.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from urllib.parse import quote

from sqlalchemy import DateTime, delete, func, update
from sqlalchemy.exc import IntegrityError

from celery_manager.alerts.senders import SENDERS, AlertMessage, DeliveryError
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import alerts as control
from celery_manager.control.audit import record as record_audit
from celery_manager.control.broker import broker_health as _broker_health
from celery_manager.control.depth_history import growth as depth_growth
from celery_manager.control.depth_history import window_samples
from celery_manager.control.latency import percentile, wait_samples
from celery_manager.control.missed import list_missed_runs
from celery_manager.control.queues import list_queues as _list_queues
from celery_manager.control.rabbitmq import nodes_for_alerts as _rabbitmq_nodes
from celery_manager.control.workers import inspect_workers as _inspect_workers
from celery_manager.db.models import (
    AlertChannel,
    AlertIncident,
    AlertRule,
    AlertWorkerSighting,
    QueueDepthSample,
    TaskRun,
)
from celery_manager.schedulers.service import entries_for

logger = logging.getLogger(__name__)

# A worker that hasn't answered for this long is taken to be gone on
# purpose (scaled down, renamed) and is forgotten, which resolves its
# worker_offline incident. Without it, every retired hostname would fire
# for ever.
WORKER_FORGET_AFTER = timedelta(hours=24)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def _matches(target: str, name: str) -> bool:
    return target == "*" or fnmatchcase(name, target)


class CheckUnavailable(Exception):
    """The rule can't be judged this pass. The text is shown on the rule."""


@dataclass
class Observation:
    """What one rule found: `firing` maps each key that breaches the rule to
    its details; `unknown` keys could not be judged and keep whatever
    incident they have."""

    firing: dict[str, dict] = field(default_factory=dict)
    unknown: set[str] = field(default_factory=set)


class _Probe:
    """Broker and worker answers for one pass, fetched at most once however
    many rules need them, and only if some rule does."""

    def __init__(self, session, celery_app, config, now, inspect_workers_fn, list_queues_fn, broker_health_fn=None,
                 rabbitmq_nodes_fn=None):
        self.session = session
        self.celery_app = celery_app
        self.config = config
        self.now = now
        self._inspect = inspect_workers_fn
        self._list_queues = list_queues_fn
        self._broker_health = broker_health_fn or _broker_health
        self._queues = None
        self._sightings = None
        self._broker = None
        self._rabbitmq_nodes_fn = rabbitmq_nodes_fn or _rabbitmq_nodes
        self._rabbitmq_nodes = None

    def rabbitmq_nodes(self) -> list[dict]:
        """RabbitMQ nodes with their alarm flags, from the management API."""
        if self._rabbitmq_nodes is None:
            try:
                self._rabbitmq_nodes = self._rabbitmq_nodes_fn(self.config)
            except Exception as exc:  # noqa: BLE001 - not configured, unreachable, 401: all "can't judge"
                raise CheckUnavailable(f"RabbitMQ management API: {exc}") from None
        return self._rabbitmq_nodes

    def queues(self) -> dict:
        if self._queues is None:
            try:
                self._queues = self._list_queues(self.celery_app, self.session, include_workers=False)
            except Exception as exc:  # noqa: BLE001 - list_queues reports trouble itself; this is belt and braces
                self._queues = {"broker": {"error": f"could not list queues: {exc}"}, "queues": []}
        return self._queues

    def broker(self) -> dict:
        if self._broker is None:
            try:
                self._broker = self._broker_health(self.celery_app)
            except Exception as exc:  # noqa: BLE001 - broker_health never raises; belt and braces
                self._broker = {"reachable": False, "error": f"could not check the broker: {exc}"}
        return self._broker

    def sightings(self) -> dict[str, datetime]:
        """`{worker: last seen}` for every remembered worker, after recording
        the workers that answer now and forgetting long-gone ones."""
        if self._sightings is None:
            try:
                online = [w["name"] for w in self._inspect(self.celery_app, self.config.inspect_timeout_seconds)
                          if w.get("online", True)]
            except Exception as exc:  # noqa: BLE001 - WorkerControlError, or anything a broker raises
                raise CheckUnavailable(f"could not ping the workers: {exc}") from None
            _record_sightings(self.session, online, self.now)
            self._sightings = {
                row.worker: _aware(row.last_seen_at) for row in self.session.query(AlertWorkerSighting).all()
            }
        return self._sightings


def _record_sightings(session, online: list[str], now: datetime) -> None:
    for name in online:
        result = session.execute(
            update(AlertWorkerSighting).where(AlertWorkerSighting.worker == name).values(last_seen_at=now)
        )
        session.commit()
        if result.rowcount:
            continue
        session.add(AlertWorkerSighting(worker=name, first_seen_at=now, last_seen_at=now))
        try:
            session.commit()
        except IntegrityError:  # another evaluator recorded it first
            session.rollback()
    session.execute(delete(AlertWorkerSighting).where(AlertWorkerSighting.last_seen_at < now - WORKER_FORGET_AFTER))
    session.commit()


# --- the checks ---------------------------------------------------------------


def _check_failure_rate(rule: AlertRule, params: dict, probe: _Probe) -> Observation:
    since = probe.now - timedelta(minutes=params["window_minutes"])
    # Finished runs only: in-flight, skipped and revoked runs say nothing
    # about reliability (the same ratio the Insights page uses).
    ts = func.coalesce(TaskRun.finished_at, TaskRun.started_at, type_=DateTime(timezone=True))
    query = probe.session.query(TaskRun.task_name, TaskRun.status, func.count()).filter(
        ts >= since, ts <= probe.now, TaskRun.status.in_(("succeeded", "failed"))
    )
    if not any(ch in rule.target for ch in "*?["):
        query = query.filter(TaskRun.task_name == rule.target)
    counts: dict[str, dict[str, int]] = {}
    for task_name, status, count in query.group_by(TaskRun.task_name, TaskRun.status).all():
        if _matches(rule.target, task_name):
            counts.setdefault(task_name, {"succeeded": 0, "failed": 0})[status] = count
    observation = Observation()
    for task_name, c in counts.items():
        runs = c["succeeded"] + c["failed"]
        if runs >= params["min_runs"] and c["failed"] / runs >= params["rate"]:
            observation.firing[task_name] = {
                "failed": c["failed"],
                "runs": runs,
                "failure_rate": round(c["failed"] / runs, 4),
                "threshold": params["rate"],
                "window_minutes": params["window_minutes"],
            }
    return observation


def _check_queue_backlog(rule: AlertRule, params: dict, probe: _Probe) -> Observation:
    data = probe.queues()
    broker = data.get("broker") or {}
    observation = Observation()
    matching = [q for q in data.get("queues") or [] if _matches(rule.target, q["name"])]
    if broker.get("error") and not any(q.get("depth") is not None for q in matching):
        raise CheckUnavailable(f"queue depth unavailable: {broker['error']}")
    for queue in matching:
        depth = queue.get("depth")
        if depth is None:
            observation.unknown.add(queue["name"])
        elif depth >= params["depth"]:
            observation.firing[queue["name"]] = {"depth": depth, "threshold": params["depth"]}
    return observation


def _check_worker_offline(rule: AlertRule, params: dict, probe: _Probe) -> Observation:
    observation = Observation()
    limit = timedelta(minutes=params["minutes"])
    for worker, last_seen in probe.sightings().items():
        if not _matches(rule.target, worker):
            continue
        gone = probe.now - last_seen
        if gone >= limit:
            observation.firing[worker] = {
                "last_seen_at": _iso(last_seen),
                "offline_minutes": int(gone.total_seconds() // 60),
                "threshold_minutes": params["minutes"],
            }
    return observation


def _check_missed_run(rule: AlertRule, params: dict, probe: _Probe) -> Observation:
    observation = Observation()
    since = probe.now - timedelta(minutes=params["window_minutes"])
    for name in entries_for(probe.session, probe.celery_app):
        if not _matches(rule.target, name):
            continue
        missed = list_missed_runs(
            probe.session, probe.celery_app, name, since=since,
            grace_seconds=probe.config.missed_run_grace_seconds, now=probe.now,
        )
        if missed:
            observation.firing[name] = {
                "missed": len(missed),
                "last_due_at": _iso(missed[-1]["due_at"]),
                "window_minutes": params["window_minutes"],
            }
    return observation


# --- broker checks -----------------------------------------------------------------

BROKER_KEY = "broker"

# When each rule first saw the broker unreachable, `{(id(celery_app), rule id): time}`.
# In memory on purpose (see docs/alerts.md): the only cost of losing it is an
# alert up to `minutes` late after an evaluator restart, and it avoids a
# table for one timestamp. Cleared the moment the broker answers.
_broker_down_since: dict[tuple[int, int], datetime] = {}


def _check_broker_unreachable(rule: AlertRule, params: dict, probe: _Probe) -> Observation:
    health = probe.broker()
    state_key = (id(probe.celery_app), rule.id)
    observation = Observation()
    if health.get("reachable"):
        _broker_down_since.pop(state_key, None)
        return observation
    since = _broker_down_since.setdefault(state_key, probe.now)
    down = probe.now - since
    if down >= timedelta(minutes=params["minutes"]):
        observation.firing[BROKER_KEY] = {
            "unreachable_minutes": int(down.total_seconds() // 60),
            "threshold_minutes": params["minutes"],
            "error": health.get("error"),
        }
    return observation


def _check_broker_memory(rule: AlertRule, params: dict, probe: _Probe) -> Observation:
    health = probe.broker()
    if not health.get("reachable"):
        raise CheckUnavailable(f"broker memory unavailable: {health.get('error') or 'the broker is unreachable'}")
    if health.get("transport") != "redis":
        raise CheckUnavailable(f"broker memory is only checked on Redis, not {health.get('transport')}")
    details = health.get("details") or {}
    observation = Observation()
    ratio = details.get("memory_ratio")
    # maxmemory 0 (no limit) gives no ratio: nothing to approach, never fires.
    if ratio is not None and ratio >= params["ratio"]:
        observation.firing[BROKER_KEY] = {
            "used_memory": details.get("used_memory"),
            "maxmemory": details.get("maxmemory"),
            "ratio": ratio,
            "threshold": params["ratio"],
        }
    return observation


# --- queue wait / depth history block ---------------------------------------------


def _check_queue_wait(rule: AlertRule, params: dict, probe: _Probe) -> Observation:
    window = timedelta(minutes=params["window_minutes"])
    samples = wait_samples(probe.session, now=probe.now, window=window)
    observation = Observation()
    for queue, waits in samples.by_queue.items():
        if not _matches(rule.target, queue) or len(waits) < params["min_runs"]:
            continue
        value = percentile(sorted(waits), params["quantile"])
        if value >= params["seconds"]:
            observation.firing[queue] = {
                "wait_seconds": round(value, 3),
                "quantile": params["quantile"],
                "runs": len(waits),
                "threshold": params["seconds"],
                "window_minutes": params["window_minutes"],
            }
    return observation


def _check_queue_growing(rule: AlertRule, params: dict, probe: _Probe) -> Observation:
    window = timedelta(minutes=params["minutes"])
    since = probe.now - window
    known = probe.session.query(QueueDepthSample.queue).filter(
        QueueDepthSample.sampled_at >= probe.now - timedelta(hours=24)
    ).distinct().all()
    names = [q for (q,) in known if _matches(rule.target, q)]
    if not known:
        raise CheckUnavailable(
            "no queue depth samples yet (needs the events listener running with queue_sample_interval_seconds above 0)"
        )
    observation = Observation()
    for name in names:
        series = window_samples(probe.session, name, since, probe.now)
        # Too little data to judge (the sampler stopped, or just started) is
        # unknown, so an open incident isn't resolved by a gap in sampling.
        # Needs 3 readings spanning at least half the window, the newest of
        # them no older than half the window.
        if (
            len(series) < 3
            or series[-1][0] - series[0][0] < window / 2
            or probe.now - series[-1][0] > window / 2
        ):
            observation.unknown.add(name)
            continue
        found = depth_growth([depth for _, depth in series], params["min_increase"])
        if found:
            observation.firing[name] = {**found, "minutes": params["minutes"], "threshold": params["min_increase"]}
    return observation


# --- RabbitMQ checks ---


def _check_rabbitmq_alarm(rule: AlertRule, params: dict, probe: _Probe) -> Observation:
    observation = Observation()
    for node in probe.rabbitmq_nodes():
        name = str(node.get("name") or "")
        if not name or not _matches(rule.target, name):
            continue
        alarms = [kind for kind, flag in (("memory", "mem_alarm"), ("disk", "disk_free_alarm")) if node.get(flag)]
        if alarms:
            observation.firing[name] = {
                "alarms": alarms,
                "mem_used": node.get("mem_used"),
                "mem_limit": node.get("mem_limit"),
                "disk_free": node.get("disk_free"),
                "disk_free_limit": node.get("disk_free_limit"),
            }
    return observation


_CHECKS = {
    "failure_rate": _check_failure_rate,
    "queue_backlog": _check_queue_backlog,
    "worker_offline": _check_worker_offline,
    "missed_run": _check_missed_run,
    "broker_unreachable": _check_broker_unreachable,
    "broker_memory": _check_broker_memory,
    "queue_wait": _check_queue_wait,
    "queue_growing": _check_queue_growing,
    "rabbitmq_alarm": _check_rabbitmq_alarm,
}


from celery_manager.alerts.depth import PARAMS as DEPTH_PARAMS
from celery_manager.alerts.depth import check as check_depth

_CHECKS.update({kind: check_depth for kind in DEPTH_PARAMS})

# --- messages ------------------------------------------------------------------------


def _summary(rule: AlertRule, key: str, details: dict, event: str) -> str:
    kind = rule.kind
    if kind in DEPTH_PARAMS:
        if kind == "daily_digest":
            return f"Daily task digest: {details.get('total', 0)} completed runs ({details.get('until')})"
        label = kind.replace("_", " ")
        return f"{key}: {label}{' resolved' if event == 'resolved' else ''}"
    if kind == "failure_rate":
        if event == "resolved":
            return f"{key}: failure rate is back under {details.get('threshold', 0):.0%}"
        return (f"{key}: {details['failed']} of {details['runs']} runs failed in the last "
                f"{details['window_minutes']} min ({details['failure_rate']:.0%})")
    if kind == "queue_backlog":
        if event == "resolved":
            return f"Queue {key} is back under {details.get('threshold')} waiting messages"
        return f"Queue {key} has {details['depth']} waiting messages (threshold {details['threshold']})"
    if kind == "worker_offline":
        if event == "resolved":
            return f"Worker {key} is answering again (or was forgotten after {WORKER_FORGET_AFTER.days * 24} h)"
        return f"Worker {key} has not answered for {details['offline_minutes']} min"
    if kind == "missed_run":
        if event == "resolved":
            return f"Schedule {key} has no missed runs in the last {details.get('window_minutes')} min"
        runs = "run" if details["missed"] == 1 else "runs"
        return f"Schedule {key} missed {details['missed']} {runs} in the last {details['window_minutes']} min"
    if kind == "broker_unreachable":
        if event == "resolved":
            return "The broker is reachable again"
        return f"The broker has been unreachable for {details['unreachable_minutes']} min: {details.get('error') or 'no answer'}"
    if kind == "broker_memory":
        if event == "resolved":
            return f"Broker memory is back under {details.get('threshold', 0):.0%} of maxmemory"
        return (f"Broker memory is at {details['ratio']:.0%} of maxmemory "
                f"({details['used_memory']:,} of {details['maxmemory']:,} bytes; threshold {details['threshold']:.0%})")
    if kind == "queue_wait":
        if event == "resolved":
            return f"Queue {key} wait time is back under {details.get('threshold'):g}s"
        return (f"Queue {key}: p{details['quantile']} wait is {details['wait_seconds']:g}s over the last "
                f"{details['window_minutes']} min (threshold {details['threshold']:g}s, {details['runs']} runs)")
    if kind == "queue_growing":
        if event == "resolved":
            return f"Queue {key} is no longer growing"
        return (f"Queue {key} grew from {details['from']} to {details['to']} waiting messages in the last "
                f"{details['minutes']} min")
    if kind == "rabbitmq_alarm":
        if event == "resolved":
            return f"RabbitMQ node {key} has no memory or disk alarm any more"
        return (f"RabbitMQ node {key} has a {' and '.join(details.get('alarms') or ['resource'])} alarm: "
                "publishers are blocked until it clears")
    return f"{rule.name}: {key}"


def dashboard_link(config: CeleryManagerConfig, kind: str | None, key: str | None) -> str | None:
    """The dashboard page about `key`, or None without `config.dashboard_url`."""
    if not config.dashboard_url:
        return None
    base = config.dashboard_url.rstrip("/") + "/#/"
    if kind == "failure_rate" and key:
        return base + "task-types/" + quote(key, safe="")
    if kind == "missed_run" and key:
        return base + "schedules/" + quote(key, safe="")
    if kind in ("queue_backlog", "broker_unreachable", "broker_memory", "queue_wait", "queue_growing",
                "rabbitmq_alarm"):
        return base + "queues"
    if kind == "worker_offline":
        return base + "workers"
    return base + "alerts"


def _message(rule: AlertRule, incident: AlertIncident, event: str, config, now: datetime) -> AlertMessage:
    details = json.loads(incident.details) if incident.details else {}
    return AlertMessage(
        event=event,
        summary=_summary(rule, incident.key, details, event),
        rule={"id": rule.id, "name": rule.name, "kind": rule.kind, "target": rule.target},
        key=incident.key,
        details=details,
        opened_at=_iso(incident.opened_at),
        resolved_at=_iso(incident.resolved_at),
        url=dashboard_link(config, rule.kind, incident.key),
        sent_at=_iso(now),
    )


def deliver(session, channels: list[AlertChannel], message: AlertMessage, config, senders=None, now=None) -> dict[int, str]:
    """Send `message` to each channel; returns `{channel id: error}` for the
    ones that failed. One channel's failure (or hang, bounded by the send
    timeout) never stops the others."""
    senders = senders or SENDERS
    errors: dict[int, str] = {}
    for channel in channels:
        try:
            senders[channel.kind](json.loads(channel.config or "{}"), message, config)
        except DeliveryError as exc:
            errors[channel.id] = str(exc)
        except Exception as exc:  # a sender bug must not take the evaluator down
            logger.exception("celery-manager alerts: channel %r failed unexpectedly", channel.name)
            errors[channel.id] = f"unexpected error: {exc}"
        if channel.id in errors:
            logger.warning("celery-manager alerts: channel %r: %s", channel.name, errors[channel.id])
        control.record_delivery(session, channel.id, errors.get(channel.id), now)
    return errors


def _channels_of(session, rule: AlertRule) -> list[AlertChannel]:
    ids = json.loads(rule.channel_ids or "[]")
    if not ids:
        return []
    return session.query(AlertChannel).filter(AlertChannel.id.in_(ids)).order_by(AlertChannel.id).all()


def _describe_errors(channels: list[AlertChannel], errors: dict[int, str]) -> str | None:
    names = {c.id: c.name for c in channels}
    return "; ".join(f"{names.get(i, i)}: {msg}" for i, msg in errors.items()) or None


# --- incidents -------------------------------------------------------------------------


def _open_incident(session, rule: AlertRule, key: str, details: dict, now: datetime) -> AlertIncident | None:
    open_key = f"{rule.id}:{key}"
    session.add(AlertIncident(
        rule_id=rule.id, key=key, state="firing", open_key=open_key,
        opened_at=now, last_seen_at=now, details=json.dumps(details),
    ))
    try:
        session.commit()
    except IntegrityError:
        # Another evaluator opened it a moment ago (or the rule was just
        # deleted, failing the foreign key): use whatever is there.
        session.rollback()
    return session.query(AlertIncident).filter(AlertIncident.open_key == open_key).one_or_none()


def _last_announced(session, rule_id: int, key: str) -> datetime | None:
    value = (
        session.query(func.max(AlertIncident.notified_at))
        .filter(AlertIncident.rule_id == rule_id, AlertIncident.key == key)
        .scalar()
    )
    return _aware(value)


def _notify_firing(session, rule, incident, config, senders, now, sent: list) -> None:
    last = _last_announced(session, rule.id, incident.key)
    if last is not None and now - last < timedelta(minutes=rule.quiet_period_minutes):
        return  # held back by the quiet period; retried on later passes
    claimed = session.execute(
        update(AlertIncident)
        .where(AlertIncident.id == incident.id, AlertIncident.notified_at.is_(None), AlertIncident.state == "firing")
        .values(notified_at=now)
    ).rowcount
    session.commit()
    if not claimed:
        return  # another evaluator is sending it
    channels = _channels_of(session, rule)
    errors = deliver(session, channels, _message(rule, incident, "firing", config, now), config, senders, now)
    if channels and len(errors) == len(channels):
        # Nobody got it: release the claim so the next pass tries again.
        session.execute(
            update(AlertIncident)
            .where(AlertIncident.id == incident.id, AlertIncident.notified_at == now)
            .values(notified_at=None, delivery_error=_describe_errors(channels, errors))
        )
    else:
        session.execute(
            update(AlertIncident).where(AlertIncident.id == incident.id)
            .values(delivery_error=_describe_errors(channels, errors))
        )
    session.commit()
    sent.append({"rule_id": rule.id, "key": incident.key, "event": "firing",
                 "channels": [c.id for c in channels], "errors": errors})


def _resolve(session, rule, incident, config, senders, now, sent: list) -> None:
    resolved = session.execute(
        update(AlertIncident)
        .where(AlertIncident.id == incident.id, AlertIncident.state == "firing")
        .values(state="resolved", open_key=None, resolved_at=now)
    ).rowcount
    session.commit()
    if not resolved:
        return
    session.refresh(incident)
    # Never announced (held by the quiet period, or every channel failed):
    # a "resolved" for an alert nobody saw is noise.
    if incident.notified_at is None or not rule.notify_resolved:
        return
    claimed = session.execute(
        update(AlertIncident)
        .where(AlertIncident.id == incident.id, AlertIncident.resolved_notified_at.is_(None))
        .values(resolved_notified_at=now)
    ).rowcount
    session.commit()
    if not claimed:
        return
    channels = _channels_of(session, rule)
    errors = deliver(session, channels, _message(rule, incident, "resolved", config, now), config, senders, now)
    if errors:
        session.execute(
            update(AlertIncident).where(AlertIncident.id == incident.id)
            .values(delivery_error=_describe_errors(channels, errors))
        )
        session.commit()
    sent.append({"rule_id": rule.id, "key": incident.key, "event": "resolved",
                 "channels": [c.id for c in channels], "errors": errors})


def _evaluate_rule(session, rule: AlertRule, probe: _Probe, config, senders, sent: list) -> None:
    now = probe.now
    params = control.validate_params(rule.kind, json.loads(rule.params or "{}"))
    observation = _CHECKS[rule.kind](rule, params, probe)

    open_incidents = {
        i.key: i for i in session.query(AlertIncident).filter_by(rule_id=rule.id, state="firing").all()
    }
    for key, details in observation.firing.items():
        key = key[:255]
        incident = open_incidents.get(key)
        if incident is None:
            incident = _open_incident(session, rule, key, details, now)
            if incident is None:
                continue
        else:
            session.execute(
                update(AlertIncident).where(AlertIncident.id == incident.id)
                .values(last_seen_at=now, details=json.dumps(details))
            )
            session.commit()
            session.refresh(incident)
        if incident.notified_at is None:
            _notify_firing(session, rule, incident, config, senders, now, sent)
    for key, incident in open_incidents.items():
        if key in observation.firing or key in observation.unknown:
            continue
        _resolve(session, rule, incident, config, senders, now, sent)


def evaluate_once(
    session_factory,
    celery_app,
    config: CeleryManagerConfig,
    now: datetime | None = None,
    *,
    senders: dict | None = None,
    inspect_workers_fn=None,
    list_queues_fn=None,
    broker_health_fn=None,
    rabbitmq_nodes_fn=None,
) -> list[dict]:
    """Judge every enabled rule once. Returns one entry per message sent
    (`rule_id`, `key`, `event`, `channels`, `errors`), for logs and tests.

    `senders` maps a channel kind to a callable `(channel config, message,
    config)`, `inspect_workers_fn` and `list_queues_fn` stand in for
    `control.workers.inspect_workers`, `control.queues.list_queues` and
    `control.broker.broker_health`, and `rabbitmq_nodes_fn` for
    `control.rabbitmq.nodes_for_alerts`; all default to the real thing and
    exist for tests.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    sent: list[dict] = []
    with session_factory() as session:
        probe = _Probe(
            session, celery_app, config, now,
            inspect_workers_fn or _inspect_workers, list_queues_fn or _list_queues, broker_health_fn,
            rabbitmq_nodes_fn,
        )
        rules = session.query(AlertRule).filter(AlertRule.enabled.is_(True)).order_by(AlertRule.id).all()
        for rule in rules:
            error = None
            try:
                _evaluate_rule(session, rule, probe, config, senders, sent)
            except CheckUnavailable as exc:
                error = str(exc)
            except Exception as exc:  # one broken rule must not silence the rest
                session.rollback()
                logger.exception("celery-manager alerts: rule %r failed", rule.name)
                error = f"unexpected error: {exc}"
            session.execute(
                update(AlertRule).where(AlertRule.id == rule.id)
                .values(last_checked_at=now, last_check_error=error)
            )
            session.commit()
    return sent


def send_test(session, actor: str, channel_id: int, config: CeleryManagerConfig, senders=None) -> None:
    """Send a test message to one channel now. Raises DeliveryError with the
    channel's answer if it fails; either way the outcome is recorded on the
    channel and in the audit log."""
    channel = control.get_channel(session, channel_id)
    now = datetime.now(timezone.utc)
    message = AlertMessage(
        event="test",
        summary=f"Test message for the alert channel {channel.name!r}, sent by {actor}",
        url=dashboard_link(config, None, None),
        sent_at=_iso(now),
    )
    errors = deliver(session, [channel], message, config, senders, now)
    record_audit(session, actor, "test_alert_channel", "alert_channel", channel.name,
                 {"id": channel.id, "ok": not errors})
    if errors:
        raise DeliveryError(errors[channel.id])


# --- running it -------------------------------------------------------------------------


def run_forever(session_factory, celery_app, config: CeleryManagerConfig, interval: float | None = None,
                stop_event: threading.Event | None = None) -> None:
    """Evaluate every `interval` seconds (default `config.alert_interval_seconds`)
    until `stop_event` is set. A failed pass is logged and the loop goes on."""
    interval = interval if interval is not None else config.alert_interval_seconds
    stop_event = stop_event or threading.Event()
    logger.info("celery-manager alerts: evaluating every %gs", interval)
    while not stop_event.is_set():
        try:
            for entry in evaluate_once(session_factory, celery_app, config):
                logger.info("celery-manager alerts: sent %s for rule %s, %s", entry["event"], entry["rule_id"], entry["key"])
        except Exception:  # e.g. the database is briefly unreachable
            logger.exception("celery-manager alerts: evaluation pass failed")
        stop_event.wait(interval)


def start_in_background(session_factory, celery_app, config: CeleryManagerConfig,
                        interval: float | None = None) -> tuple[threading.Thread, threading.Event]:
    """Run `run_forever` on a daemon thread, e.g. next to the events
    listener. Set the returned event to stop it."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_forever, args=(session_factory, celery_app, config, interval, stop_event),
        name="cm-alerts", daemon=True,
    )
    thread.start()
    return thread, stop_event
