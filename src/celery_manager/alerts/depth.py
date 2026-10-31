"""Production alert checks. Insufficient evidence is unknown, never recovery."""
from collections import Counter
from datetime import timedelta
from fnmatch import fnmatchcase
from statistics import quantiles

from sqlalchemy import or_
from sqlalchemy.orm import load_only

from celery_manager.alerts.host_health import PARAMS as HOST_HEALTH_PARAMS
from celery_manager.alerts.host_health import check as check_host_health
from celery_manager.control.health import aware, beat_health
from celery_manager.db.models import TaskRun, WorkerState

PARAMS = {
    "task_failed": {"window_minutes": (int, 60, 1, 10080)},
    "consecutive_failures": {"count": (int, 3, 1, 1000), "window_minutes": (int, 1440, 1, 43200)},
    "run_stuck": {"seconds": (float, 3600, 1, 604800), "p95_multiplier": (float, 3, 1, 100), "min_runs": (int, 10, 2, 10000)},
    "worker_lost": {"seconds": (float, 120, 1, 86400)},
    "beat_stale": {"seconds": (float, 120, 30, 86400)},
    "task_sla": {"seconds": (float, 3600, 1, 604800), "window_minutes": (int, 60, 1, 10080)},
    "duration_regression": {"multiplier": (float, 2, 1.01, 100), "baseline_days": (int, 30, 1, 365), "recent_minutes": (int, 60, 1, 43200), "min_runs": (int, 10, 2, 10000)},
    "daily_digest": {"hour_utc": (int, 8, 0, 23)},
    "run_late": {"seconds": (float, 300, 1, 86400), "window_minutes": (int, 60, 1, 10080)},
    **HOST_HEALTH_PARAMS,
}


def _types():
    from celery_manager.alerts.evaluator import CheckUnavailable, Observation
    return CheckUnavailable, Observation


def _rows(rule, params, probe):
    unavailable, _ = _types()
    query = probe.session.query(TaskRun).options(load_only(
        TaskRun.id, TaskRun.task_id, TaskRun.task_name, TaskRun.status, TaskRun.started_at,
        TaskRun.finished_at, TaskRun.runtime_seconds, TaskRun.worker,
        TaskRun.occurrence_at, TaskRun.schedule_name,
    ))
    # Apply time/target scope in SQL before the safety cap. Retaining unrelated
    # historic runs must not disable every alert in a production deployment.
    if not any(c in rule.target for c in "*?["):
        query = query.filter(TaskRun.task_name == rule.target)
    elif "[" not in rule.target:
        pattern = rule.target.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(TaskRun.task_name.like(pattern.replace("*", "%").replace("?", "_"), escape="\\"))
    if rule.kind in ("task_failed", "task_sla", "consecutive_failures"):
        cutoff = probe.now - timedelta(minutes=params["window_minutes"])
        query = query.filter(or_(TaskRun.finished_at >= cutoff,
                                 TaskRun.finished_at.is_(None) & TaskRun.status.in_(("failed", "succeeded"))))
    elif rule.kind == "worker_lost":
        query = query.filter(TaskRun.status == "started")
    elif rule.kind == "run_stuck":
        query = query.filter(or_(TaskRun.status == "started", (TaskRun.status == "succeeded") &
                                 (TaskRun.finished_at >= probe.now - timedelta(days=30))))
    elif rule.kind == "duration_regression":
        start = probe.now - timedelta(minutes=params["recent_minutes"], days=params["baseline_days"])
        query = query.filter(TaskRun.status == "succeeded", TaskRun.finished_at >= start)
    elif rule.kind == "run_late":
        query = query.filter(TaskRun.occurrence_at >= probe.now - timedelta(minutes=params["window_minutes"]))
    elif rule.kind == "daily_digest":
        end = probe.now.replace(hour=params["hour_utc"], minute=0, second=0, microsecond=0)
        if end > probe.now:
            end -= timedelta(days=1)
        query = query.filter(TaskRun.finished_at >= end - timedelta(days=1), TaskRun.finished_at < end)
    rows = query.order_by(TaskRun.id.desc()).limit(20001).all()
    if not rows and rule.kind not in ("run_stuck", "worker_lost", "daily_digest"):
        raise unavailable("No recorded task runs in this rule's scope; the rule cannot be judged")
    if len(rows) > 20000:
        raise unavailable("Rule scope exceeds 20,000 runs; narrow the target/window or use external metrics")
    return rows


def _matched(rule, rows):
    return [r for r in rows if fnmatchcase(r.task_name, rule.target)]


def _p95(values):
    return quantiles(values, n=100, method="inclusive")[94]


def check(rule, params, probe):
    if rule.kind in HOST_HEALTH_PARAMS:
        return check_host_health(rule, params, probe)
    unavailable, observation = _types()
    result = observation()
    if rule.kind == "beat_stale":
        health = beat_health(probe.session, stale_seconds=params["seconds"], now=probe.now)
        if health["state"] == "unknown":
            raise unavailable(health["reason"])
        if health["state"] == "stale":
            result.firing["beat"] = health
        return result
    rows = _matched(rule, _rows(rule, params, probe))
    if not rows and probe.session.query(TaskRun.id).first() is None:
        raise unavailable("No recorded runs match this rule")
    if rule.kind in ("task_failed", "task_sla"):
        cutoff = probe.now - timedelta(minutes=params["window_minutes"])
        for run in rows:
            finished = aware(run.finished_at)
            if finished is None:
                if run.status in ("failed", "succeeded"):
                    result.unknown.add(run.task_id)
                continue
            if not cutoff <= finished <= probe.now:
                continue
            if rule.kind == "task_sla" and run.runtime_seconds is None:
                result.unknown.add(run.task_id)
                continue
            breach = run.status == "failed" if rule.kind == "task_failed" else (
                run.runtime_seconds is not None and run.runtime_seconds > params["seconds"])
            if breach:
                result.firing[run.task_id] = {"task_name": run.task_name, "task_id": run.task_id,
                                              "runtime_seconds": run.runtime_seconds, "status": run.status}
        return result
    if rule.kind == "consecutive_failures":
        cutoff = probe.now - timedelta(minutes=params["window_minutes"])
        names = {r.task_name for r in rows}
        for name in names:
            completed = sorted([r for r in rows if r.task_name == name and r.status in ("failed", "succeeded")
                                and r.finished_at is not None and cutoff <= aware(r.finished_at) <= probe.now],
                               key=lambda r: (aware(r.finished_at), r.id), reverse=True)
            if len(completed) < params["count"]:
                result.unknown.add(name)
            elif all(r.status == "failed" for r in completed[:params["count"]]):
                result.firing[name] = {"consecutive_failures": params["count"], "task_id": completed[0].task_id}
        return result
    if rule.kind in ("run_stuck", "worker_lost"):
        workers = {w.hostname: w for w in probe.session.query(WorkerState).all()}
        baselines = {}
        for historical in rows:
            if historical.status == "succeeded" and historical.runtime_seconds is not None and historical.finished_at is not None and probe.now - timedelta(days=30) <= aware(historical.finished_at) <= probe.now:
                baselines.setdefault(historical.task_name, []).append(historical.runtime_seconds)
        thresholds = {name: max(params.get("seconds", 0), _p95(values) * params.get("p95_multiplier", 1))
                      for name, values in baselines.items() if len(values) >= params.get("min_runs", 2)}
        for run in rows:
            if run.status != "started":
                continue
            if rule.kind == "worker_lost":
                worker = workers.get(run.worker)
                if worker is None:
                    result.unknown.add(run.task_id)
                elif worker.state == "offline" or (probe.now - aware(worker.last_seen_at)).total_seconds() > params["seconds"]:
                    result.firing[run.task_id] = {"task_name": run.task_name, "worker": run.worker,
                                                "last_seen_at": aware(worker.last_seen_at).isoformat()}
                continue
            if run.started_at is None:
                result.unknown.add(run.task_id)
                continue
            threshold = thresholds.get(run.task_name, params["seconds"])
            elapsed = (probe.now - aware(run.started_at)).total_seconds()
            if elapsed > threshold:
                result.firing[run.task_id] = {"task_name": run.task_name, "runtime_seconds": elapsed, "threshold": threshold}
        return result
    if rule.kind == "duration_regression":
        boundary = probe.now - timedelta(minutes=params["recent_minutes"])
        start = boundary - timedelta(days=params["baseline_days"])
        for name in {r.task_name for r in rows}:
            values = [r for r in rows if r.task_name == name and r.status == "succeeded" and r.runtime_seconds is not None and r.finished_at]
            baseline = [r.runtime_seconds for r in values if start <= aware(r.finished_at) < boundary]
            recent = [r.runtime_seconds for r in values if boundary <= aware(r.finished_at) <= probe.now]
            if len(baseline) < params["min_runs"] or len(recent) < params["min_runs"]:
                result.unknown.add(name)
                continue
            old, new = _p95(baseline), _p95(recent)
            if old <= 0:
                result.unknown.add(name)
            elif new > old * params["multiplier"]:
                result.firing[name] = {"baseline_p95": old, "recent_p95": new, "ratio": new / old,
                                       "baseline_runs": len(baseline), "recent_runs": len(recent)}
        return result
    if rule.kind == "daily_digest":
        end = probe.now.replace(hour=params["hour_utc"], minute=0, second=0, microsecond=0)
        if end > probe.now:
            end -= timedelta(days=1)
        begin = end - timedelta(days=1)
        completed = [r for r in rows if r.finished_at and begin <= aware(r.finished_at) < end]
        counts = Counter(r.status for r in completed)
        tasks = {name: dict(Counter(r.status for r in completed if r.task_name == name))
                 for name in sorted({r.task_name for r in completed})}
        result.firing["digest:" + end.isoformat()] = {"from": begin.isoformat(), "until": end.isoformat(),
                                                      "counts": dict(counts), "tasks": tasks, "total": sum(counts.values())}
        return result
    if rule.kind == "run_late":
        # Cron-monitor "late": a scheduled occurrence that started more than
        # `seconds` after it was due, or has not started that long after.
        # A run that never arrived at all is the missed_run rule's job.
        for run in rows:
            due = aware(run.occurrence_at)
            if due is None or due > probe.now:
                continue
            started = aware(run.started_at)
            if started is None and run.status in ("succeeded", "failed", "skipped", "revoked"):
                result.unknown.add(run.task_id)
                continue
            delay = ((started or probe.now) - due).total_seconds()
            if delay > params["seconds"]:
                result.firing[run.task_id] = {"task_name": run.task_name, "task_id": run.task_id,
                                              "schedule": run.schedule_name, "due_at": due.isoformat(),
                                              "started": started is not None, "late_seconds": round(delay, 3)}
        return result
    raise unavailable("Unsupported production rule")
