"""Explicit schedule ownership and non-destructive reconciliation.

A plan never deletes backend entries. An apply checks the backend snapshot hash
again and rechecks each target before writing; external backends cannot provide a
cross-backend transaction, so an operational failure can leave partial progress.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from celery.schedules import solar

from celery_manager.schedule_specs import spec_from_schedule, validate_spec
from celery_manager.schedulers.base import ScheduleEntry


class ReconcileConflict(ValueError):
    """The backend changed after the operator reviewed the plan."""


def serialize_entry(entry):
    if getattr(entry.schedule, "relative", False):
        raise ValueError("Relative interval rounding cannot be exported losslessly")
    spec = spec_from_schedule(entry.schedule)
    if spec is None and isinstance(entry.schedule, solar):
        spec = {"type": "solar", "event": entry.schedule.event, "latitude": entry.schedule.lat,
                "longitude": entry.schedule.lon}
    if spec is None:
        raise ValueError(f"Schedule {entry.name!r} cannot be exported losslessly: unsupported schedule type")
    options = dict(entry.options)
    if isinstance(options.get("expires"), datetime):
        options["expires"] = options["expires"].isoformat()
    value = {"name": entry.name, "task": entry.task, "schedule": spec, "args": entry.args,
             "kwargs": entry.kwargs, "options": options, "enabled": entry.enabled}
    # Fail instead of inventing reprs for data that cannot be restored.
    json.dumps(value, allow_nan=False)
    return value


def deserialize_entry(value, app=None):
    allowed = {"name", "task", "schedule", "args", "kwargs", "options", "enabled"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Invalid schedule export entry")
    json.dumps(value, allow_nan=False)
    if not isinstance(value.get("name"), str) or not value["name"] or not isinstance(value.get("task"), str) or not value["task"]:
        raise ValueError("Every schedule needs a nonempty name and task")
    args, kwargs, options = value.get("args", []), value.get("kwargs", {}), value.get("options", {})
    if not isinstance(args, list) or not isinstance(kwargs, dict) or not isinstance(options, dict):
        raise TypeError("Invalid schedule arguments or options")
    if not isinstance(value.get("enabled", True), bool):
        raise TypeError("enabled must be a boolean")
    spec = value.get("schedule")
    if isinstance(spec, dict) and spec.get("type") == "solar":
        if set(spec) != {"type", "event", "latitude", "longitude"}:
            raise ValueError("Invalid solar schedule")
        schedule = solar(spec["event"], spec["latitude"], spec["longitude"], app=app)
    else:
        _, schedule = validate_spec(spec, app=app)
    return ScheduleEntry(value["name"], value["task"], schedule, args, kwargs, options, value.get("enabled", True))


def _snapshot(entries):
    result = {}
    for entry in entries:
        if entry.name in result:
            raise ValueError(f"Duplicate schedule name: {entry.name}")
        result[entry.name] = serialize_entry(entry)
    return result


def _fingerprint(snapshot):
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def export_backend(backend):
    entries = _snapshot(backend.list())
    return {"version": 1, "backend": backend.name, "fingerprint": _fingerprint(entries),
            "entries": [entries[name] for name in sorted(entries)]}


def load_export(document, app=None):
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("entries"), list):
        raise ValueError("Expected a version-1 schedule export")
    entries = [deserialize_entry(item, app) for item in document["entries"]]
    _snapshot(entries)
    return entries


def plan_reconcile(backend, desired, *, ownership="backend", allow_updates=False):
    if ownership not in ("backend", "code", "sql"):
        raise ValueError("ownership must be backend, code or sql")
    if ownership == "sql" and backend.name != "sqlalchemy-beat":
        raise ValueError("SQL ownership requires SQLAlchemyBeatBackend")
    observed = _snapshot(backend.list())
    desired = _snapshot(desired)
    changes = []
    for name in sorted(set(observed) | set(desired)):
        before, after = observed.get(name), desired.get(name)
        if before == after:
            continue
        action = "create" if before is None else "extra" if after is None else "update"
        # Backend ownership is observation-only: do not overwrite its truth.
        writable = ownership != "backend" and action != "extra" and (action == "create" or allow_updates)
        changes.append({"name": name, "action": action, "apply": writable, "before": before, "after": after})
    return {"version": 1, "backend": backend.name, "ownership": ownership,
            "observed_fingerprint": _fingerprint(observed), "changes": changes}


def apply_reconcile(backend, plan, *, app=None):
    if not backend.writable:
        raise PermissionError("Scheduler backend is read-only")
    if not isinstance(plan, dict) or plan.get("version") != 1 or plan.get("backend") != backend.name:
        raise ValueError("Plan does not target this backend")
    ownership = plan.get("ownership")
    if ownership not in ("backend", "code", "sql") or (ownership == "sql" and backend.name != "sqlalchemy-beat"):
        raise ValueError("Invalid ownership mode")
    # Validate the whole plan before the first write, including entry shapes.
    operations = []
    names = set()
    if not isinstance(plan.get("changes"), list):
        raise TypeError("Plan changes must be a list")
    for change in plan["changes"]:
        if not isinstance(change, dict) or not isinstance(change.get("name"), str) or change["name"] in names:
            raise ValueError("Invalid or duplicate plan change")
        names.add(change["name"])
        if not change.get("apply"):
            continue
        if ownership == "backend" or change.get("action") not in ("create", "update"):
            raise ValueError("This plan contains a forbidden operation")
        entry = deserialize_entry(change["after"], app)
        if entry.name != change.get("name"):
            raise ValueError("Plan entry name mismatch")
        operations.append((change, entry))
    current = _snapshot(backend.list())
    if _fingerprint(current) != plan.get("observed_fingerprint"):
        raise ReconcileConflict("The scheduler backend changed; generate and review a new plan")
    applied = []
    for change, entry in operations:
        current_entry = backend.get(entry.name)
        if (serialize_entry(current_entry) if current_entry else None) != change.get("before"):
            raise ReconcileConflict(f"Schedule {entry.name!r} changed during reconciliation")
        if change["action"] == "create":
            backend.create(entry)
        else:
            backend.update(entry.name, task=entry.task, schedule=entry.schedule, args=entry.args,
                           kwargs=entry.kwargs, options=entry.options, enabled=entry.enabled)
        applied.append(entry.name)
    return {"applied": applied, "deleted": [], "ownership": ownership}


def copy_schedules(source, target, *, app=None, apply=False, allow_updates=False):
    """Copy every definition from one scheduler backend into another.

    The migration path between backends, e.g. RedBeat or code's
    `beat_schedule` into the SQL scheduler. Always builds a reviewed-style plan
    first (`plan_reconcile` with the target's natural ownership); with
    `apply=False` (the default) nothing is written and the plan is returned
    for review. Entries only in the target are reported as `extra` and never
    deleted; entries that differ are only overwritten with `allow_updates`.
    Run-state (last run, counters) is not copied, so the target starts each
    schedule fresh.
    """
    desired = load_export(export_backend(source), app)
    ownership = "sql" if target.name == "sqlalchemy-beat" else "code"
    plan = plan_reconcile(target, desired, ownership=ownership, allow_updates=allow_updates)
    if not apply:
        return {"plan": plan, "applied": [], "dry_run": True}
    result = apply_reconcile(target, plan, app=app)
    return {"plan": plan, "applied": result["applied"], "dry_run": False}
