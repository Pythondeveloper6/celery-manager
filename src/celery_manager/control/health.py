"""Operational findings are advisory and never rewrite task execution status."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from celery_manager.db.models import AppSetting, TaskRun, WorkerState


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _beat_row(row, stale_seconds, now):
    if row is None:
        return {"state": "unknown", "last_tick": None, "age_seconds": None,
                "reason": "Configure celery_manager_db_url on the stamping beat scheduler to record liveness."}
    try:
        data = json.loads(row.value)
        stamp = aware(datetime.fromisoformat(data["at"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"state": "unknown", "last_tick": None, "age_seconds": None, "reason": "Invalid stored beat heartbeat"}
    age = max(0, (now - stamp).total_seconds())
    return {"state": "stale" if age > stale_seconds else "healthy", "last_tick": stamp.isoformat(),
            "age_seconds": age, "scheduler": data.get("scheduler"), "hostname": data.get("hostname"),
            "owner_id": data.get("owner_id")}


def beat_health(session, *, stale_seconds=120, now=None, owner_id=None):
    from celery_manager.beat_health import OWNER_PREFIX, owner_key
    now = aware(now or datetime.now(timezone.utc))
    if owner_id is not None:
        key = owner_key(owner_id)
        return _beat_row(session.get(AppSetting, key), stale_seconds, now)
    result = _beat_row(session.get(AppSetting, "beat_last_tick"), stale_seconds, now)
    rows = session.query(AppSetting).filter(AppSetting.key.startswith(OWNER_PREFIX, autoescape=True)).order_by(AppSetting.key).all()
    owners = [_beat_row(row, stale_seconds, now) for row in rows]
    if owners:
        # A healthy owner must not hide another codebase's stopped scheduler.
        result["state"] = ("stale" if any(item["state"] == "stale" for item in owners)
                           else "unknown" if any(item["state"] == "unknown" for item in owners) else "healthy")
    result["owners"] = owners
    return result


def attention_runs(session, *, stuck_seconds=3600, worker_stale_seconds=120, now=None, limit=100):
    now = now or datetime.now(timezone.utc)
    workers = {w.hostname: w for w in session.query(WorkerState).all()}
    findings = []
    runs = session.query(TaskRun).filter(TaskRun.status == "started").order_by(TaskRun.started_at).limit(5000).all()
    for run in runs:
        started = aware(run.started_at)
        runtime = max(0, (now - started).total_seconds()) if started else None
        worker = workers.get(run.worker)
        worker_lost = worker is not None and (worker.state == "offline" or (
            now - aware(worker.last_seen_at)).total_seconds() > worker_stale_seconds)
        if worker_lost or (runtime is not None and runtime > stuck_seconds):
            findings.append({"task_id": run.task_id, "task_name": run.task_name, "worker": run.worker,
                             "kind": "worker_lost" if worker_lost else "suspect_stuck", "runtime_seconds": runtime})
            if len(findings) >= limit:
                break
    return findings
