"""Alert rules and channels: validation, create/update/delete, and the shapes
the API returns. The evaluator that acts on them is `celery_manager.alerts`.

Secrets are write-only. A channel's webhook URL (a Slack incoming webhook
URL *is* the credential: anyone holding it can post) and its signing secret
go in, and never come back out: `channel_entry` returns the URL reduced to
scheme and host, and only whether a secret is set. An update that leaves a
secret field out keeps the stored value, so the dashboard can edit a
channel's name or recipients without ever having seen its URL.

Every change writes an audit row. The audit details name the fields that
changed, never their values, for the same reason.

Errors: ValueError for bad input, LookupError for an unknown id,
:class:`AlertConflict` for a duplicate name.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import update

from celery_manager.control.audit import record as record_audit
from celery_manager.db.models import AlertChannel, AlertIncident, AlertRule

RULE_KINDS = (
    "failure_rate", "queue_backlog", "worker_offline", "missed_run",
    "broker_unreachable", "broker_memory", "queue_wait", "queue_growing",
)
# Kinds about the one broker rather than a named thing: their target is
# always "*" whatever was sent, so a rule can't claim to watch "redis-2".
BROKER_KINDS = ("broker_unreachable", "broker_memory")
CHANNEL_KINDS = ("slack", "webhook", "email")

MAX_NAME_LENGTH = 100
MAX_TARGET_LENGTH = 255
MAX_EMAIL_RECIPIENTS = 20
MAX_URL_LENGTH = 2000
MAX_SECRET_LENGTH = 500
# A week: longer than that is "never tell me again", which is a disabled rule.
MAX_MINUTES = 7 * 24 * 60

# Per kind: parameter -> (type, default, minimum, maximum). Every parameter
# has a default, so `{}` is a valid starting point for any kind.
RULE_PARAMS: dict[str, dict[str, tuple[type, float, float, float]]] = {
    # Fires when at least `min_runs` runs finished in the last
    # `window_minutes` and at least `rate` of them failed.
    "failure_rate": {
        "rate": (float, 0.2, 0.01, 1.0),
        "min_runs": (int, 10, 1, 1_000_000),
        "window_minutes": (int, 15, 1, MAX_MINUTES),
    },
    # Fires when the broker holds at least `depth` messages in the queue.
    "queue_backlog": {
        "depth": (int, 1000, 1, 1_000_000_000),
    },
    # Fires when a worker seen before has not answered a ping for `minutes`.
    "worker_offline": {
        "minutes": (int, 5, 1, MAX_MINUTES),
    },
    # Fires while a firing of the schedule due in the last `window_minutes`
    # never arrived (after `missed_run_grace_seconds`).
    "missed_run": {
        "window_minutes": (int, 60, 1, MAX_MINUTES),
    },
    # Fires once the broker has refused connections for `minutes`.
    "broker_unreachable": {
        "minutes": (int, 2, 1, MAX_MINUTES),
    },
    # Fires when Redis uses at least `ratio` of its maxmemory. Never fires
    # when maxmemory is 0 (no limit: no ceiling to approach).
    "broker_memory": {
        "ratio": (float, 0.8, 0.01, 1.0),
    },
    # --- queue wait / depth history block ---
    # Fires when at least `min_runs` runs started in the last `window_minutes`
    # and the `quantile`-th percentile (95 = p95) of their queue wait, which
    # excludes ETA/countdown time, is at least `seconds`.
    "queue_wait": {
        "seconds": (float, 60.0, 0.001, 86_400.0),
        "quantile": (int, 95, 1, 100),
        "window_minutes": (int, 15, 1, MAX_MINUTES),
        "min_runs": (int, 5, 1, 1_000_000),
    },
    # Fires when the sampled depth rose by at least `min_increase` over the
    # last `minutes` without draining on the way (see
    # control.depth_history.growth for the exact rule).
    "queue_growing": {
        "minutes": (int, 15, 1, MAX_MINUTES),
        "min_increase": (int, 50, 1, 1_000_000_000),
    },
}

# Email addresses are only shape-checked: the mail server is the authority.
# What matters here is that nothing can smuggle a header (CR/LF) or a second
# recipient (comma) into the To line.
_EMAIL = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")


class AlertConflict(Exception):
    """The request is well-formed but clashes with an existing row."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return value.isoformat()


def _loads(text: str | None, fallback):
    try:
        value = json.loads(text) if text else fallback
    except ValueError:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


# --- validation ----------------------------------------------------------------


def _validate_name(name, what: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"the {what} needs a name")
    name = name.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"the {what} name can be at most {MAX_NAME_LENGTH} characters")
    return name


def _validate_minutes(value, what: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be a whole number of minutes")  # noqa: TRY004 - API input; ValueError is a 422
    if not minimum <= value <= MAX_MINUTES:
        raise ValueError(f"{what} must be between {minimum} and {MAX_MINUTES} minutes")
    return value


def validate_params(kind: str, params) -> dict:
    """The kind's parameters with defaults filled in. Unknown keys are
    refused rather than ignored, so a typo ("treshold") is not silently a
    rule that fires on the default."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")  # noqa: TRY004
    spec = RULE_PARAMS[kind]
    unknown = sorted(set(params) - set(spec))
    if unknown:
        raise ValueError(f"unknown parameter for a {kind} rule: {', '.join(unknown)} (expected {', '.join(spec)})")
    clean: dict = {}
    for key, (typ, default, low, high) in spec.items():
        value = params.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")  # noqa: TRY004
        if typ is int:
            if isinstance(value, float) and not value.is_integer():
                raise ValueError(f"{key} must be a whole number")
            value = int(value)
        else:
            value = float(value)
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low:g} and {high:g}")
        clean[key] = value
    return clean


def _validate_target(target) -> str:
    if target is None:
        return "*"
    if not isinstance(target, str) or not target.strip():
        raise ValueError("the target must be a name or a pattern; use * for all")
    target = target.strip()
    if len(target) > MAX_TARGET_LENGTH:
        raise ValueError(f"the target can be at most {MAX_TARGET_LENGTH} characters")
    return target


def _validate_channel_ids(session, channel_ids) -> list[int]:
    if channel_ids is None:
        return []
    if not isinstance(channel_ids, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in channel_ids):
        raise ValueError("channel_ids must be a list of channel ids")
    ids = sorted(set(channel_ids))
    if ids:
        found = {row[0] for row in session.query(AlertChannel.id).filter(AlertChannel.id.in_(ids)).all()}
        missing = [i for i in ids if i not in found]
        if missing:
            raise ValueError(f"no alert channel with id {', '.join(map(str, missing))}")
    return ids


def validate_http_url(url, what: str = "URL") -> str:
    """An http(s) URL with a host. These are entered by an admin and fetched
    by the server, so the scheme is pinned: no file:, ftp:, gopher: or
    anything else urllib would happily open. (Which *hosts* are reachable is
    the deployment's network policy; see docs/alerts.md.)"""
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"the {what} is required")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"the {what} can be at most {MAX_URL_LENGTH} characters")
    if any(ch.isspace() for ch in url):
        raise ValueError(f"the {what} can't contain spaces")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"the {what} must start with https:// (or http://) and name a host")
    return url


def _validate_recipients(to) -> list[str]:
    if isinstance(to, str):
        to = [part for part in re.split(r"[,\s]+", to) if part]
    if not isinstance(to, list) or not to or not all(isinstance(a, str) for a in to):
        raise ValueError("an email channel needs at least one recipient address")
    clean = []
    for address in to:
        address = address.strip()
        if not _EMAIL.match(address):
            raise ValueError(f"{address!r} is not an email address")
        if address not in clean:
            clean.append(address)
    if len(clean) > MAX_EMAIL_RECIPIENTS:
        raise ValueError(f"an email channel can have at most {MAX_EMAIL_RECIPIENTS} recipients")
    return clean


def _validate_channel_config(kind: str, config, existing: dict | None) -> dict:
    """The config to store. `existing` is the stored config on update: a
    secret field left out (or null) keeps its stored value."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("config must be an object")  # noqa: TRY004
    existing = existing or {}
    allowed = {"slack": {"url"}, "webhook": {"url", "secret"}, "email": {"to"}}[kind]
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValueError(f"unknown setting for a {kind} channel: {', '.join(unknown)}")
    if kind == "email":
        return {"to": _validate_recipients(config.get("to", existing.get("to")))}
    label = "Slack webhook URL" if kind == "slack" else "webhook URL"
    url = config.get("url")
    clean = {"url": validate_http_url(url if url is not None else existing.get("url"), label)}
    if kind == "webhook":
        secret = config.get("secret")
        if secret is None:
            secret = existing.get("secret")
        elif not isinstance(secret, str) or len(secret) > MAX_SECRET_LENGTH:
            raise ValueError(f"the signing secret must be text of at most {MAX_SECRET_LENGTH} characters")
        # "" clears it: unsigned delivery.
        if secret:
            clean["secret"] = secret
    return clean


# --- shapes the API returns ------------------------------------------------------


def mask_url(url: str | None) -> str | None:
    """`https://hooks.slack.com/…`: enough to recognise the channel, nothing
    that would let a viewer post to it (the path and query carry the token)."""
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}/…" if parts.hostname else "…"


def channel_entry(channel: AlertChannel, rule_count: int | None = None) -> dict:
    stored = _loads(channel.config, {})
    if channel.kind == "email":
        config: dict = {"to": stored.get("to", [])}
    else:
        config = {"url": mask_url(stored.get("url"))}
        if channel.kind == "webhook":
            config["secret_set"] = bool(stored.get("secret"))
    entry = {
        "id": channel.id,
        "name": channel.name,
        "kind": channel.kind,
        "config": config,
        "created_by": channel.created_by,
        "created_at": _iso(channel.created_at),
        "last_sent_at": _iso(channel.last_sent_at),
        "last_error": channel.last_error,
        "last_error_at": _iso(channel.last_error_at),
    }
    if rule_count is not None:
        entry["rule_count"] = rule_count
    return entry


def rule_entry(rule: AlertRule, open_incidents: list[AlertIncident] | None = None) -> dict:
    open_incidents = open_incidents or []
    return {
        "id": rule.id,
        "name": rule.name,
        "kind": rule.kind,
        "target": rule.target,
        "params": _loads(rule.params, {}),
        "channel_ids": _loads(rule.channel_ids, []),
        "quiet_period_minutes": rule.quiet_period_minutes,
        "notify_resolved": bool(rule.notify_resolved),
        "enabled": bool(rule.enabled),
        "created_by": rule.created_by,
        "created_at": _iso(rule.created_at),
        "updated_at": _iso(rule.updated_at),
        "last_checked_at": _iso(rule.last_checked_at),
        "last_check_error": rule.last_check_error,
        # "disabled", "firing" (at least one open incident) or "ok".
        "state": "disabled" if not rule.enabled else ("firing" if open_incidents else "ok"),
        "firing_keys": sorted(i.key for i in open_incidents),
    }


def incident_entry(incident: AlertIncident, rule: AlertRule | None) -> dict:
    return {
        "id": incident.id,
        "rule_id": incident.rule_id,
        "rule_name": rule.name if rule else None,
        "rule_kind": rule.kind if rule else None,
        "key": incident.key,
        "state": incident.state,
        "opened_at": _iso(incident.opened_at),
        "last_seen_at": _iso(incident.last_seen_at),
        "notified_at": _iso(incident.notified_at),
        "resolved_at": _iso(incident.resolved_at),
        "details": _loads(incident.details, {}),
        "delivery_error": incident.delivery_error,
    }


# --- reads ----------------------------------------------------------------------


def _open_incidents_by_rule(session) -> dict[int, list[AlertIncident]]:
    grouped: dict[int, list[AlertIncident]] = {}
    for incident in session.query(AlertIncident).filter(AlertIncident.state == "firing").all():
        grouped.setdefault(incident.rule_id, []).append(incident)
    return grouped


def list_rules(session) -> list[dict]:
    open_by_rule = _open_incidents_by_rule(session)
    rules = session.query(AlertRule).order_by(AlertRule.name).all()
    return [rule_entry(rule, open_by_rule.get(rule.id)) for rule in rules]


def list_channels(session) -> list[dict]:
    counts: dict[int, int] = {}
    for (ids,) in session.query(AlertRule.channel_ids).all():
        for channel_id in _loads(ids, []):
            counts[channel_id] = counts.get(channel_id, 0) + 1
    channels = session.query(AlertChannel).order_by(AlertChannel.name).all()
    return [channel_entry(c, counts.get(c.id, 0)) for c in channels]


def list_incidents(session, state: str | None = None, rule_id: int | None = None, limit: int = 100) -> list[dict]:
    """Newest first. `state` is "firing" or "resolved"; None for both."""
    if state not in (None, "firing", "resolved"):
        raise ValueError("state must be firing or resolved")
    query = session.query(AlertIncident)
    if state is not None:
        query = query.filter(AlertIncident.state == state)
    if rule_id is not None:
        query = query.filter(AlertIncident.rule_id == rule_id)
    incidents = query.order_by(AlertIncident.opened_at.desc(), AlertIncident.id.desc()).limit(limit).all()
    rules = {r.id: r for r in session.query(AlertRule).filter(AlertRule.id.in_({i.rule_id for i in incidents})).all()}
    return [incident_entry(i, rules.get(i.rule_id)) for i in incidents]


def get_channel(session, channel_id: int) -> AlertChannel:
    channel = session.get(AlertChannel, channel_id)
    if channel is None:
        raise LookupError(f"no alert channel with id {channel_id}")
    return channel


def _get_rule(session, rule_id: int) -> AlertRule:
    rule = session.get(AlertRule, rule_id)
    if rule is None:
        raise LookupError(f"no alert rule with id {rule_id}")
    return rule


# --- channels ---------------------------------------------------------------------


def _name_taken(session, model, name: str, exclude_id: int | None = None) -> bool:
    query = session.query(model.id).filter(model.name == name)
    if exclude_id is not None:
        query = query.filter(model.id != exclude_id)
    return query.first() is not None


def create_channel(session, actor: str, name, kind, config) -> dict:
    name = _validate_name(name, "channel")
    if kind not in CHANNEL_KINDS:
        raise ValueError(f"the channel kind must be one of {', '.join(CHANNEL_KINDS)}")
    clean = _validate_channel_config(kind, config, None)
    if _name_taken(session, AlertChannel, name):
        raise AlertConflict(f"an alert channel named {name!r} already exists")
    channel = AlertChannel(name=name, kind=kind, config=json.dumps(clean), created_by=actor, created_at=_now())
    session.add(channel)
    session.commit()
    record_audit(session, actor, "create_alert_channel", "alert_channel", name, {"id": channel.id, "kind": kind})
    return channel_entry(channel, 0)


def update_channel(session, actor: str, channel_id: int, changes: dict) -> dict:
    """`changes` may hold `name` and `config`; the kind is fixed (a Slack
    channel becoming an email channel is a different channel)."""
    channel = get_channel(session, channel_id)
    changed: list[str] = []
    if changes.get("name") is not None:
        name = _validate_name(changes["name"], "channel")
        if name != channel.name:
            if _name_taken(session, AlertChannel, name, exclude_id=channel.id):
                raise AlertConflict(f"an alert channel named {name!r} already exists")
            channel.name = name
            changed.append("name")
    if changes.get("config") is not None:
        old = _loads(channel.config, {})
        new = _validate_channel_config(channel.kind, changes["config"], old)
        changed.extend(sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k)))
        channel.config = json.dumps(new)
    session.commit()
    if changed:
        record_audit(session, actor, "update_alert_channel", "alert_channel", channel.name,
                     {"id": channel.id, "changed": changed})
    return channel_entry(channel)


def delete_channel(session, actor: str, channel_id: int) -> None:
    channel = get_channel(session, channel_id)
    # Take it off every rule that sends to it, so no rule points at nothing.
    for rule in session.query(AlertRule).all():
        ids = _loads(rule.channel_ids, [])
        if channel.id in ids:
            rule.channel_ids = json.dumps([i for i in ids if i != channel.id])
    name = channel.name
    session.delete(channel)
    session.commit()
    record_audit(session, actor, "delete_alert_channel", "alert_channel", name, {"id": channel_id})


def record_delivery(session, channel_id: int, error: str | None, now: datetime | None = None) -> None:
    """Store the outcome of a delivery on the channel for the dashboard.
    A plain UPDATE, so a channel deleted meanwhile is simply not found."""
    now = now or _now()
    values = {"last_error": error, "last_error_at": now} if error else {"last_sent_at": now, "last_error": None}
    session.execute(update(AlertChannel).where(AlertChannel.id == channel_id).values(**values))
    session.commit()


# --- rules --------------------------------------------------------------------------


def create_rule(session, actor: str, payload: dict) -> dict:
    name = _validate_name(payload.get("name"), "rule")
    kind = payload.get("kind")
    if kind not in RULE_KINDS:
        raise ValueError(f"the rule kind must be one of {', '.join(RULE_KINDS)}")
    params = validate_params(kind, payload.get("params"))
    target = "*" if kind in BROKER_KINDS else _validate_target(payload.get("target"))
    quiet = _validate_minutes(payload.get("quiet_period_minutes", 60), "the quiet period")
    channel_ids = _validate_channel_ids(session, payload.get("channel_ids"))
    if _name_taken(session, AlertRule, name):
        raise AlertConflict(f"an alert rule named {name!r} already exists")
    rule = AlertRule(
        name=name,
        kind=kind,
        target=target,
        params=json.dumps(params),
        channel_ids=json.dumps(channel_ids),
        quiet_period_minutes=quiet,
        notify_resolved=bool(payload.get("notify_resolved", True)),
        enabled=bool(payload.get("enabled", True)),
        created_by=actor,
        created_at=_now(),
    )
    session.add(rule)
    session.commit()
    record_audit(session, actor, "create_alert_rule", "alert_rule", name,
                 {"id": rule.id, "kind": kind, "target": target, "params": params})
    return rule_entry(rule)


def _close_open_incidents(session, rule_id: int, now: datetime) -> None:
    """Resolve a rule's open incidents without a message: the rule stopped
    being watched (disabled, or its condition changed), which is not the
    problem going away."""
    session.execute(
        update(AlertIncident)
        .where(AlertIncident.rule_id == rule_id, AlertIncident.state == "firing")
        .values(state="resolved", open_key=None, resolved_at=now)
    )


def update_rule(session, actor: str, rule_id: int, changes: dict) -> dict:
    """Partial update; keys left out keep their value. Changing the kind or
    target, or disabling the rule, closes its open incidents silently: what
    they described is no longer being watched."""
    rule = _get_rule(session, rule_id)
    changed: dict = {}
    kind = changes.get("kind") or rule.kind
    if kind not in RULE_KINDS:
        raise ValueError(f"the rule kind must be one of {', '.join(RULE_KINDS)}")
    if changes.get("name") is not None:
        name = _validate_name(changes["name"], "rule")
        if name != rule.name:
            if _name_taken(session, AlertRule, name, exclude_id=rule.id):
                raise AlertConflict(f"an alert rule named {name!r} already exists")
            changed["name"] = name
    if kind != rule.kind:
        changed["kind"] = kind
    if changes.get("params") is not None or kind != rule.kind:
        # A new kind without params starts from its own defaults, not the
        # old kind's keys.
        params = validate_params(kind, changes.get("params"))
        if params != _loads(rule.params, {}):
            changed["params"] = params
    if kind in BROKER_KINDS:
        if rule.target != "*":
            changed["target"] = "*"
    elif changes.get("target") is not None:
        target = _validate_target(changes["target"])
        if target != rule.target:
            changed["target"] = target
    if changes.get("quiet_period_minutes") is not None:
        quiet = _validate_minutes(changes["quiet_period_minutes"], "the quiet period")
        if quiet != rule.quiet_period_minutes:
            changed["quiet_period_minutes"] = quiet
    if changes.get("channel_ids") is not None:
        ids = _validate_channel_ids(session, changes["channel_ids"])
        if ids != _loads(rule.channel_ids, []):
            changed["channel_ids"] = ids
    for flag in ("notify_resolved", "enabled"):
        if changes.get(flag) is not None and bool(changes[flag]) != bool(getattr(rule, flag)):
            changed[flag] = bool(changes[flag])
    if not changed:
        return rule_entry(rule, session.query(AlertIncident).filter_by(rule_id=rule.id, state="firing").all())

    now = _now()
    for key, value in changed.items():
        setattr(rule, key, json.dumps(value) if key in ("params", "channel_ids") else value)
    rule.updated_at = now
    if {"kind", "target"} & set(changed) or changed.get("enabled") is False:
        _close_open_incidents(session, rule.id, now)
    session.commit()
    record_audit(session, actor, "update_alert_rule", "alert_rule", rule.name, {"id": rule.id, "changes": changed})
    return rule_entry(rule, session.query(AlertIncident).filter_by(rule_id=rule.id, state="firing").all())


def delete_rule(session, actor: str, rule_id: int) -> None:
    rule = _get_rule(session, rule_id)
    name = rule.name
    # Its incident history goes with it: an incident without its rule
    # can't say what fired.
    session.query(AlertIncident).filter(AlertIncident.rule_id == rule.id).delete(synchronize_session=False)
    session.delete(rule)
    session.commit()
    record_audit(session, actor, "delete_alert_rule", "alert_rule", name, {"id": rule_id})
