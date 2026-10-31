"""Observe persisted host health; never invoke a host callback in alert evaluation."""
from fnmatch import fnmatchcase

from celery_manager.health_probes import HealthScopeUnavailable, probe_status
from celery_manager.scheduler_health import scheduler_status

PARAMS = {
    "custom_probe": {"stale_seconds": (float, 120, 1, 86400)},
    "scheduler_stale": {"seconds": (float, 120, 1, 86400)},
}


def check(rule, params, probe):
    from celery_manager.alerts.evaluator import CheckUnavailable, Observation
    observation = Observation()
    try:
        if rule.kind == "custom_probe":
            rows = probe_status(probe.session, now=probe.now, freshness_seconds=params["stale_seconds"],
                                expected_names=[item.name for item in getattr(probe.config, "health_probes", ())])
            key_field = "name"
        else:
            rows = scheduler_status(probe.session, now=probe.now, stale_seconds=params["seconds"])
            key_field = "instance_id"
    except HealthScopeUnavailable as exc:
        raise CheckUnavailable(str(exc)) from exc
    rows = [row for row in rows if fnmatchcase(row[key_field], rule.target)]
    if not rows:
        raise CheckUnavailable("No registered health observations match this rule")
    for row in rows:
        key = row[key_field]
        if row["state"] == "unknown":
            observation.unknown.add(key)
        elif row["state"] == "degraded":
            observation.firing[key] = row
    return observation
